from __future__ import annotations

import asyncio

import numpy as np
import pytest

from harbor_voice.domain import (
    ActionKind,
    ActionProposal,
    AudioRecording,
    MessageResponse,
    ProposalResponse,
    Transcript,
)
from harbor_voice.meeting.agent import (
    NO_ACTIONS_IN_MEETINGS,
    MeetingAgent,
    MeetingAwareBackend,
    MeetingConfig,
)
from harbor_voice.meeting.audio import MeetingAudioBridge, find_device
from harbor_voice.meeting.session import MeetingDevices, MeetingSession
from harbor_voice.meeting.transcript import (
    ME,
    PARTICIPANT,
    MeetingTranscript,
    addresses_agent,
    is_stop_command,
)
from harbor_voice.meeting.vad import EnergySegmenter, to_mono_16k
from tests.fakes import FakeBackend, FakeSpeaker


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class ScriptedTranscriber:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)

    async def transcribe(self, recording: AudioRecording) -> Transcript:
        del recording
        return Transcript(self.texts.pop(0))


def tone(ms: int, amplitude: int = 4_000) -> bytes:
    count = 16 * ms
    t = np.arange(count) / 16_000
    return (np.sin(2 * np.pi * 220 * t) * amplitude).astype(np.int16).tobytes()


def silence(ms: int) -> bytes:
    return b"\x00\x00" * 16 * ms


def recording() -> AudioRecording:
    return AudioRecording(b"\x00\x00" * 1_600, 16_000)


# --- addressing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Scout, what is the MACC balance?", True),
        ("Hey scout can you summarise", True),
        ("Scouting is great", False),
        ("Scouts, what can you do?", True),
        ("Let's move on", False),
    ],
)
def test_wake_word_must_be_a_whole_word(text: str, expected: bool) -> None:
    assert addresses_agent(text, ["scout"]) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Scout, stop.", True),
        ("OK Scout, that's enough", True),
        ("Scout be quiet please", True),
        ("Scout, stop the migration plan and tell me why", False),
        ("Stop", False),
    ],
)
def test_stop_command_is_short_and_addressed(text: str, expected: bool) -> None:
    assert is_stop_command(text, ["scout"]) is expected


# --- transcript ---------------------------------------------------------------------------


def test_transcript_is_bounded_by_age_and_renders_offsets() -> None:
    clock = Clock()
    transcript = MeetingTranscript(max_age_seconds=60, clock=clock)
    transcript.add(PARTICIPANT, "old remark")
    clock.now += 90
    transcript.add(PARTICIPANT, "What regions do we support?")

    rendered = transcript.render()

    assert "old remark" not in rendered
    assert "[01:30] Participant: What regions do we support?" in rendered


def test_latest_question_prefers_questions_from_participants() -> None:
    clock = Clock()
    transcript = MeetingTranscript(clock=clock)
    transcript.add(PARTICIPANT, "How does pricing work?")
    transcript.add(PARTICIPANT, "That makes sense.")
    transcript.add(ME, "Can I ask something?")

    latest = transcript.latest_question()

    assert latest is not None and latest.text == "How does pricing work?"


# --- segmentation -------------------------------------------------------------------------


def test_segmenter_emits_one_utterance_after_trailing_silence() -> None:
    segmenter = EnergySegmenter()
    chunks = segmenter.feed(silence(600)) + segmenter.feed(tone(900)) + segmenter.feed(silence(900))

    assert len(chunks) == 1
    assert len(chunks[0]) >= len(tone(900))


def test_segmenter_ignores_short_clicks_and_suppressed_audio() -> None:
    segmenter = EnergySegmenter()
    assert segmenter.feed(silence(300) + tone(60) + silence(900)) == []
    segmenter.suppressed = True
    assert segmenter.feed(tone(900) + silence(900)) == []


def test_segmenter_reports_sustained_speech_once() -> None:
    fired: list[int] = []
    segmenter = EnergySegmenter(sustained_ms=300, on_sustained_speech=lambda: fired.append(1))
    segmenter.feed(silence(300) + tone(1_200) + silence(900))

    assert fired == [1]


def test_resampling_downmixes_stereo_48k_to_mono_16k() -> None:
    stereo = np.zeros(4_800 * 2, dtype=np.int16)
    converted = to_mono_16k(stereo.tobytes(), 48_000, 2)

    assert len(converted) == 1_600 * 2


# --- agent --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_participant_question_with_wake_word_is_answered_aloud() -> None:
    events: list[str] = []
    backend = FakeBackend(events, MessageResponse(kind="message", message="Two regions."))
    speaker = FakeSpeaker(events)
    agent = MeetingAgent(
        backend=backend,
        speaker=speaker,
        transcriber=ScriptedTranscriber("Scout, how many regions do we use?"),
        config=MeetingConfig(owner_name="Russell"),
    )

    await agent.handle_utterance(PARTICIPANT, recording())
    await agent.wait_idle()

    assert speaker.spoken == ["Two regions."]
    prompt = backend.requests[0].text
    assert "MEETING MODE" in prompt
    assert "Russell" in prompt
    assert "<meeting_transcript>" in prompt and "how many regions" in prompt


@pytest.mark.asyncio
async def test_unaddressed_speech_is_only_transcribed() -> None:
    events: list[str] = []
    backend = FakeBackend(events)
    speaker = FakeSpeaker(events)
    agent = MeetingAgent(
        backend=backend,
        speaker=speaker,
        transcriber=ScriptedTranscriber("We should review the budget."),
    )

    await agent.handle_utterance(PARTICIPANT, recording())

    assert backend.requests == []
    assert speaker.spoken == []
    assert len(agent.transcript) == 1


@pytest.mark.asyncio
async def test_stop_command_interrupts_without_asking_backend() -> None:
    events: list[str] = []
    backend = FakeBackend(events)
    speaker = FakeSpeaker(events)
    agent = MeetingAgent(
        backend=backend, speaker=speaker, transcriber=ScriptedTranscriber("Scout, stop.")
    )

    await agent.handle_utterance(PARTICIPANT, recording())

    assert speaker.cancel_calls == 1
    assert backend.requests == []


@pytest.mark.asyncio
async def test_proposals_are_never_executed_in_meetings() -> None:
    events: list[str] = []
    proposal = ProposalResponse(
        kind="proposal",
        action=ActionProposal(
            id="6f1c2b0e-1c7c-4d6e-9a59-2f4f7a2b3c4d",
            kind=ActionKind.OPEN_URL,
            target="https://example.com",
            summary="open",
        ),
    )
    backend = FakeBackend(events, proposal)
    speaker = FakeSpeaker(events)
    agent = MeetingAgent(
        backend=backend,
        speaker=speaker,
        transcriber=ScriptedTranscriber("Scout, open the portal"),
        config=MeetingConfig(owner_name="Russell"),
    )

    await agent.handle_utterance(PARTICIPANT, recording())
    await agent.wait_idle()

    assert speaker.spoken == [NO_ACTIONS_IN_MEETINGS.format(owner="Russell")]


@pytest.mark.asyncio
async def test_participants_can_be_ignored_while_owner_can_still_ask() -> None:
    events: list[str] = []
    backend = FakeBackend(events)
    speaker = FakeSpeaker(events)
    agent = MeetingAgent(
        backend=backend,
        speaker=speaker,
        transcriber=ScriptedTranscriber("Scout, what's new?", "Scout, answer that"),
        config=MeetingConfig(respond_to_participants=False),
    )

    await agent.handle_utterance(PARTICIPANT, recording())
    await agent.handle_utterance(ME, recording())
    await agent.wait_idle()

    assert len(backend.requests) == 1


@pytest.mark.asyncio
async def test_answer_latest_uses_last_participant_question() -> None:
    events: list[str] = []
    backend = FakeBackend(events, MessageResponse(kind="message", message="Yes."))
    speaker = FakeSpeaker(events)
    agent = MeetingAgent(
        backend=backend,
        speaker=speaker,
        transcriber=ScriptedTranscriber("Does it support private endpoints?"),
    )
    await agent.handle_utterance(PARTICIPANT, recording())

    await agent.answer_latest()

    assert "Does it support private endpoints?" in backend.requests[0].text
    assert speaker.spoken == ["Yes."]


@pytest.mark.asyncio
async def test_barge_in_only_interrupts_while_speaking() -> None:
    events: list[str] = []
    speaker = FakeSpeaker(events)
    agent = MeetingAgent(
        backend=FakeBackend(events), speaker=speaker, transcriber=ScriptedTranscriber()
    )

    agent.on_participant_speech()
    assert speaker.cancel_calls == 0
    agent._speaking = True
    agent.on_participant_speech()
    assert speaker.cancel_calls == 1


@pytest.mark.asyncio
async def test_meeting_aware_backend_adds_context_to_private_turns_only() -> None:
    from harbor_voice.domain import AssistantRequest

    events: list[str] = []
    inner = FakeBackend(events)
    backend = MeetingAwareBackend(inner)

    await backend.ask(AssistantRequest("hello"))
    backend.context_provider = lambda: "[00:01] Participant: hi"
    await backend.ask(AssistantRequest("summarise"))
    await backend.ask_raw(AssistantRequest("raw"))

    assert inner.requests[0].text == "hello"
    assert "<meeting_transcript>" in inner.requests[1].text
    assert inner.requests[2].text == "raw"
    assert backend.reset_calls == 0  # attribute access is delegated


# --- audio bridge -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_mixes_mic_and_speech_and_resolves_playback() -> None:
    bridge = MeetingAudioBridge(output_device=None, mic_device=None)
    bridge._channels = 2
    mic = np.full((4, 1), 100, dtype=np.int16)
    bridge._on_input(mic, 4, None, None)

    play = asyncio.create_task(bridge.play(np.full(3, 1_000, dtype=np.int16).tobytes()))
    await asyncio.sleep(0)
    out = np.zeros((4, 2), dtype=np.int16)
    bridge._on_output(out, 4, None, None)
    await asyncio.wait_for(play, 1)

    assert out[:, 0].tolist() == [1_100, 1_100, 1_100, 100]
    assert (out[:, 0] == out[:, 1]).all()
    assert not bridge.playing


@pytest.mark.asyncio
async def test_bridge_clear_releases_waiting_playback() -> None:
    bridge = MeetingAudioBridge(output_device=None, mic_device=None)
    play = asyncio.create_task(bridge.play(b"\x01\x00" * 100))
    await asyncio.sleep(0)

    bridge.clear()

    await asyncio.wait_for(play, 1)


def test_find_device_prefers_wasapi_and_reports_missing() -> None:
    devices = [
        {"name": "CABLE Input (VB-Audio)", "hostapi": 0, "max_output_channels": 2},
        {"name": "CABLE Input (VB-Audio)", "hostapi": 1, "max_output_channels": 2},
    ]
    apis = {0: {"name": "MME"}, 1: {"name": "Windows WASAPI"}}

    def query(index):
        return apis[index]

    assert (
        find_device(
            "cable input", kind="output", query_devices=lambda: devices, query_hostapis=query
        )
        == 1
    )
    assert find_device(None, kind="output") is None
    with pytest.raises(LookupError):
        find_device("missing", kind="output", query_devices=lambda: devices, query_hostapis=query)


# --- session ------------------------------------------------------------------------------


class FakeBridge:
    def __init__(self, on_mic_audio) -> None:
        self.on_mic_audio = on_mic_audio
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FakeListener:
    def __init__(self, on_audio) -> None:
        self.on_audio = on_audio
        self.device_label = "Headset"
        self.stopped = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_session_routes_loopback_speech_to_agent_and_cleans_up() -> None:
    events: list[str] = []
    backend = MeetingAwareBackend(
        FakeBackend(events, MessageResponse(kind="message", message="Sure."))
    )
    speaker = FakeSpeaker(events)
    bridges: list[FakeBridge] = []
    listeners: list[FakeListener] = []
    notices = []
    session = MeetingSession(
        backend=backend,
        transcriber=ScriptedTranscriber("Scout, can you help?"),
        config=MeetingConfig(announce=True),
        devices=MeetingDevices(),
        on_event=notices.append,
        bridge_factory=lambda on_mic_audio: bridges.append(FakeBridge(on_mic_audio)) or bridges[-1],
        listener_factory=lambda on_audio: listeners.append(FakeListener(on_audio)) or listeners[-1],
        speaker_factory=lambda bridge: speaker,
    )

    await session.start()
    assert backend.context_provider is not None
    listeners[0].on_audio(silence(300) + tone(900) + silence(900), 16_000, 1)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if len(speaker.spoken) >= 2:
            break
    await session.stop()

    assert "Scout" in speaker.spoken[0]  # announcement
    assert speaker.spoken[1] == "Sure."
    assert bridges[0].stopped and listeners[0].stopped
    assert backend.context_provider is None
    assert len(session.agent.transcript) == 0
    assert notices[-1].text.startswith("Meeting mode off")
