from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harbor_voice.coordinator import ApprovalExpired, ApprovalNotFound, TurnBusy, TurnCoordinator
from harbor_voice.domain import (
    ActionKind,
    ActionProposal,
    AppState,
    MessageResponse,
    ProposalResponse,
    Transcript,
)
from harbor_voice.policy import PermissionPolicy
from tests.fakes import FakeBackend, FakeRunner, FakeSpeaker, FakeTranscriber, sample_recording


@dataclass
class Rig:
    coordinator: TurnCoordinator
    transcriber: FakeTranscriber
    backend: FakeBackend
    speaker: FakeSpeaker
    runner: FakeRunner
    states: list[AppState]
    events: list[str]
    clock: list[float]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    events: list[str] = []
    states: list[AppState] = []
    clock = [100.0]
    transcriber = FakeTranscriber(events)
    backend = FakeBackend(events)
    speaker = FakeSpeaker(events)
    runner = FakeRunner(events)
    coordinator = TurnCoordinator(
        transcriber=transcriber,
        backend=backend,
        speaker=speaker,
        runner=runner,
        policy=PermissionPolicy(tmp_path, {}),
        state_sink=states.append,
        clock=lambda: clock[0],
    )
    return Rig(coordinator, transcriber, backend, speaker, runner, states, events, clock)


def proposed(kind: ActionKind, target: str, *, action_id: UUID | None = None) -> ProposalResponse:
    return ProposalResponse(
        kind="proposal",
        message="This needs approval.",
        action=ActionProposal(
            id=action_id or UUID("83a2cbaf-8a6b-4f4f-aabd-010cae50d749"),
            kind=kind,
            target=target,
            summary="Perform requested action",
            payload={},
        ),
    )


@pytest.mark.asyncio
async def test_message_turn_transcribes_asks_and_speaks(rig: Rig) -> None:
    rig.transcriber.result = Transcript("What time is it?", confidence=0.9)
    rig.backend.response = MessageResponse(kind="message", message="It is noon.")

    await rig.coordinator.submit(sample_recording())

    assert rig.backend.requests[0].text == "What time is it?"
    assert rig.speaker.spoken == ["It is noon."]
    assert rig.states == [
        AppState.TRANSCRIBING,
        AppState.THINKING,
        AppState.SPEAKING,
        AppState.IDLE,
    ]


@pytest.mark.asyncio
async def test_completed_message_turn_exposes_display_text(rig: Rig) -> None:
    rig.transcriber.result = Transcript("What time is it?", confidence=0.9)
    rig.backend.response = MessageResponse(kind="message", message="It is noon.")

    await rig.coordinator.submit(sample_recording())

    assert rig.coordinator.last_transcript == "What time is it?"
    assert rig.coordinator.last_response == "It is noon."


@pytest.mark.asyncio
async def test_confirmation_has_no_effect_before_approval(rig: Rig) -> None:
    rig.backend.response = proposed(ActionKind.OPEN_URL, "https://example.com")

    await rig.coordinator.submit(sample_recording())

    assert rig.runner.executed == []
    assert rig.coordinator.pending is not None
    assert rig.states[-1] is AppState.APPROVAL


@pytest.mark.asyncio
async def test_approval_executes_exactly_once(rig: Rig) -> None:
    rig.backend.response = proposed(ActionKind.OPEN_URL, "https://example.com")
    await rig.coordinator.submit(sample_recording())
    action_id = rig.coordinator.pending.action.id

    await rig.coordinator.approve(action_id)

    assert len(rig.runner.executed) == 1
    assert rig.speaker.spoken[-1] == "Action complete."
    with pytest.raises(ApprovalNotFound):
        await rig.coordinator.approve(action_id)
    assert len(rig.runner.executed) == 1


@pytest.mark.asyncio
async def test_rejection_has_no_effect(rig: Rig) -> None:
    rig.backend.response = proposed(ActionKind.OPEN_URL, "https://example.com")
    await rig.coordinator.submit(sample_recording())
    action_id = rig.coordinator.pending.action.id

    rig.coordinator.reject(action_id)

    assert rig.runner.executed == []
    assert rig.coordinator.pending is None
    assert rig.states[-1] is AppState.IDLE


@pytest.mark.asyncio
async def test_expired_approval_has_no_effect(rig: Rig) -> None:
    rig.backend.response = proposed(ActionKind.OPEN_URL, "https://example.com")
    await rig.coordinator.submit(sample_recording())
    action_id = rig.coordinator.pending.action.id
    rig.clock[0] = 400.001

    with pytest.raises(ApprovalExpired):
        await rig.coordinator.approve(action_id)

    assert rig.runner.executed == []
    assert rig.coordinator.pending is None


@pytest.mark.asyncio
async def test_mismatched_approval_id_has_no_effect(rig: Rig) -> None:
    rig.backend.response = proposed(ActionKind.OPEN_URL, "https://example.com")
    await rig.coordinator.submit(sample_recording())

    with pytest.raises(ApprovalNotFound):
        await rig.coordinator.approve(uuid4())

    assert rig.runner.executed == []
    assert rig.coordinator.pending is not None


@pytest.mark.asyncio
async def test_blocked_proposal_is_explained_without_effect(rig: Rig) -> None:
    rig.backend.response = proposed(ActionKind.OPEN_URL, "file:///C:/Windows/win.ini")

    await rig.coordinator.submit(sample_recording())

    assert rig.runner.executed == []
    assert "can't do that" in rig.speaker.spoken[-1]
    assert rig.coordinator.pending is None


@pytest.mark.asyncio
async def test_empty_transcript_does_not_reach_backend(rig: Rig) -> None:
    rig.transcriber.result = Transcript("  ")

    await rig.coordinator.submit(sample_recording())

    assert rig.backend.requests == []
    assert rig.states == [AppState.TRANSCRIBING, AppState.IDLE]


@pytest.mark.asyncio
async def test_new_turn_cancels_speech_before_transcription(rig: Rig) -> None:
    await rig.coordinator.submit(sample_recording())

    assert rig.events.index("speaker.cancel") < rig.events.index("transcriber.transcribe")


@pytest.mark.asyncio
async def test_overlapping_turn_is_rejected_without_replacing_state(rig: Rig) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def transcribe(recording):
        del recording
        started.set()
        await release.wait()
        return Transcript("first")

    rig.coordinator._transcriber.transcribe = transcribe
    first = asyncio.create_task(rig.coordinator.submit(sample_recording()))
    await started.wait()

    with pytest.raises(TurnBusy):
        await rig.coordinator.submit(sample_recording())

    release.set()
    await first
    assert len(rig.backend.requests) == 1


@pytest.mark.asyncio
async def test_action_failure_enters_error_without_retry(rig: Rig) -> None:
    rig.backend.response = proposed(ActionKind.OPEN_URL, "https://example.com")
    rig.runner.error = RuntimeError("browser unavailable")
    await rig.coordinator.submit(sample_recording())

    await rig.coordinator.approve(rig.coordinator.pending.action.id)

    assert len(rig.runner.executed) == 1
    assert rig.states[-1] is AppState.ERROR
    assert rig.coordinator.last_error == "browser unavailable"


@pytest.mark.asyncio
async def test_new_conversation_clears_pending_and_resets_backend(rig: Rig) -> None:
    rig.backend.response = proposed(ActionKind.OPEN_URL, "https://example.com")
    await rig.coordinator.submit(sample_recording())

    await rig.coordinator.new_conversation()

    assert rig.coordinator.pending is None
    assert rig.backend.reset_calls == 1
    assert rig.states[-1] is AppState.IDLE
