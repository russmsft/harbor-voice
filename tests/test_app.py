from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from PySide6.QtWidgets import QApplication

from harbor_voice.app import HarborRuntime, ProviderBundle, build_components
from harbor_voice.domain import (
    ActionKind,
    ActionProposal,
    ActionResult,
    AppState,
    AudioRecording,
    MessageResponse,
    ProposalResponse,
    Transcript,
)
from harbor_voice.storage import (
    AppPaths,
    AssistantSettings,
    HistoryStore,
    Retention,
    SettingsStore,
)
from harbor_voice.ui.tray import SingleInstanceGuard, TrayController
from harbor_voice.ui.window import ConversationWindow
from tests.fakes import FakeBackend, FakeRunner, FakeSpeaker, FakeTranscriber


class FakeRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.recording = AudioRecording(b"\x00\x00" * 4_000, 16_000)
        self.start_calls = 0
        self.close_calls = 0
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.close_error: Exception | None = None

    def start(self) -> None:
        self.events.append("recorder.start")
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error

    def stop(self) -> AudioRecording | None:
        self.events.append("recorder.stop")
        if self.stop_error is not None:
            raise self.stop_error
        return self.recording

    def close(self) -> None:
        self.events.append("recorder.close")
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class StartableBackend(FakeBackend):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.started: list[Path] = []

    async def start(self, workspace: Path) -> None:
        self.events.append("backend.start")
        self.started.append(workspace)


class FakeHotkey:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self) -> None:
        self.events.append("hotkey.start")

    def stop(self) -> None:
        self.events.append("hotkey.stop")


class FailingHistoryStore:
    def append(self, entry) -> None:
        del entry
        raise OSError("history unavailable")


class FakeStartupRegistration:
    def __init__(self) -> None:
        self.enabled = False
        self.value: str | None = None
        self.set_calls: list[bool] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def configured_entry(self) -> str | None:
        return self.value

    def set_enabled(self, enabled: bool) -> bool:
        changed = self.enabled != enabled
        self.enabled = enabled
        self.value = "fake-command" if enabled else None
        self.set_calls.append(enabled)
        return changed

    def restore(self, value: str | None) -> None:
        self.set_enabled(value is not None)
        self.value = value


@dataclass
class RuntimeRig:
    runtime: HarborRuntime
    events: list[str]
    backend: StartableBackend
    speaker: FakeSpeaker
    recorder: FakeRecorder
    hotkey: FakeHotkey
    startup: FakeStartupRegistration


@pytest_asyncio.fixture
async def runtime_rig(tmp_path: Path, qapp: QApplication) -> RuntimeRig:
    events: list[str] = []
    backend = StartableBackend(events)
    backend.response = MessageResponse(kind="message", message="A safe response.")
    speaker = FakeSpeaker(events)
    recorder = FakeRecorder(events)
    hotkey = FakeHotkey(events)
    startup = FakeStartupRegistration()
    providers = ProviderBundle(
        transcriber=FakeTranscriber(events, Transcript("hello")),
        backend=backend,
        speaker=speaker,
        recorder=recorder,
        runner=FakeRunner(events, ActionResult(True, "done")),
    )
    paths = AppPaths.from_root(tmp_path / "data")
    settings = AssistantSettings(workspace=tmp_path, retention=Retention.SEVEN_DAYS)
    runtime = HarborRuntime(
        application=qapp,
        loop=asyncio.get_running_loop(),
        paths=paths,
        settings=settings,
        settings_store=SettingsStore(paths.settings),
        providers=providers,
        hotkey_factory=lambda *args, **kwargs: hotkey,
        tray=TrayController(),
        window=ConversationWindow(),
        guard=SingleInstanceGuard(paths.root / "instance.lock"),
        startup_registration=startup,
    )
    return RuntimeRig(runtime, events, backend, speaker, recorder, hotkey, startup)


def test_components_use_configured_workspace(tmp_path: Path) -> None:
    events: list[str] = []
    settings = AssistantSettings(workspace=tmp_path)
    providers = ProviderBundle(
        transcriber=FakeTranscriber(events),
        backend=StartableBackend(events),
        speaker=FakeSpeaker(events),
        recorder=FakeRecorder(events),
        runner=FakeRunner(events),
    )

    graph = build_components(settings, providers, state_sink=lambda state: None)

    assert graph.policy.workspace == tmp_path.resolve()


@pytest.mark.asyncio
async def test_runtime_start_initializes_backend_before_hotkey(runtime_rig: RuntimeRig) -> None:
    assert await runtime_rig.runtime.start() is True

    assert runtime_rig.backend.started == [runtime_rig.runtime.settings.workspace]
    assert "transcriber.prepare" in runtime_rig.events
    assert runtime_rig.events.index("backend.start") < runtime_rig.events.index("hotkey.start")
    await runtime_rig.runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_failure_rolls_back_hotkey_and_instance_lock(
    runtime_rig: RuntimeRig,
) -> None:
    runtime_rig.runtime.tray.show = lambda: (_ for _ in ()).throw(
        RuntimeError("tray unavailable")
    )

    with pytest.raises(RuntimeError, match="tray unavailable"):
        await runtime_rig.runtime.start()

    assert "hotkey.stop" in runtime_rig.events
    assert runtime_rig.backend.close_calls == 1
    assert runtime_rig.runtime.guard.acquire() is True
    runtime_rig.runtime.guard.release()


@pytest.mark.asyncio
async def test_push_to_talk_cancels_speech_before_recording(runtime_rig: RuntimeRig) -> None:
    runtime_rig.runtime.handle_press()

    assert runtime_rig.events.index("speaker.cancel") < runtime_rig.events.index("recorder.start")


@pytest.mark.asyncio
async def test_mute_toggle_does_not_disable_speech_interruption(runtime_rig: RuntimeRig) -> None:
    release = asyncio.Event()

    async def speaking():
        await release.wait()

    active = runtime_rig.runtime._start_operation(speaking())
    await asyncio.sleep(0)
    runtime_rig.runtime.graph.coordinator._state = AppState.SPEAKING
    runtime_rig.runtime._set_state(AppState.SPEAKING)

    runtime_rig.runtime._toggle_mute()
    runtime_rig.runtime._toggle_mute()
    runtime_rig.runtime.handle_press()
    await asyncio.sleep(0)

    assert runtime_rig.recorder.start_calls == 1
    assert active.cancelled()


@pytest.mark.asyncio
async def test_release_submits_recording_and_updates_window(runtime_rig: RuntimeRig) -> None:
    runtime_rig.runtime.handle_press()

    task = runtime_rig.runtime.handle_release()
    await task

    assert runtime_rig.backend.requests[0].text == "hello"
    assert runtime_rig.runtime.window.transcript_text.toPlainText() == "hello"
    assert runtime_rig.runtime.window.response_text.toPlainText() == "A safe response."


@pytest.mark.asyncio
async def test_press_is_ignored_while_turn_is_still_processing(runtime_rig: RuntimeRig) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def transcribe(recording):
        del recording
        started.set()
        await release.wait()
        return Transcript("hello")

    runtime_rig.runtime.graph.coordinator._transcriber.transcribe = transcribe
    runtime_rig.runtime.handle_press()
    first = runtime_rig.runtime.handle_release()
    await started.wait()

    runtime_rig.runtime.handle_press()

    assert runtime_rig.recorder.start_calls == 1
    release.set()
    await first


def test_microphone_start_failure_is_visible_and_recoverable(runtime_rig: RuntimeRig) -> None:
    runtime_rig.recorder.start_error = RuntimeError("microphone busy")

    runtime_rig.runtime.handle_press()

    assert runtime_rig.runtime.graph.coordinator.state is AppState.ERROR
    assert "microphone busy" in runtime_rig.runtime.window.response_text.toPlainText()
    assert runtime_rig.runtime._recording is False


@pytest.mark.asyncio
async def test_expired_dialog_clears_pending_approval(runtime_rig: RuntimeRig) -> None:
    runtime_rig.backend.response = ProposalResponse(
        kind="proposal",
        action=ActionProposal(
            id=UUID("2f28c747-f8d8-4f85-a398-51962ec8ef8b"),
            kind=ActionKind.OPEN_URL,
            target="https://example.com",
            summary="Open Example",
        ),
    )
    runtime_rig.runtime.handle_press()
    await runtime_rig.runtime.handle_release()
    dialog = runtime_rig.runtime._approval_dialog
    assert dialog is not None

    dialog.expire()

    assert runtime_rig.runtime.graph.coordinator.pending is None
    assert runtime_rig.runtime.graph.coordinator.state is AppState.IDLE
    assert runtime_rig.runtime._approval_dialog is None


def test_saving_settings_applies_launch_at_login(runtime_rig: RuntimeRig) -> None:
    updated = runtime_rig.runtime.settings.model_copy(update={"launch_at_login": True})

    runtime_rig.runtime._save_settings(updated)

    assert runtime_rig.startup.set_calls == [True]
    assert runtime_rig.runtime.settings_store.load().launch_at_login is True


def test_failed_settings_save_rolls_back_launch_registration(
    runtime_rig: RuntimeRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated = runtime_rig.runtime.settings.model_copy(update={"launch_at_login": True})

    def fail(settings) -> None:
        del settings
        raise OSError("disk full")

    monkeypatch.setattr(runtime_rig.runtime.settings_store, "save", fail)

    runtime_rig.runtime._save_settings(updated)

    assert runtime_rig.startup.set_calls == [True, False]
    assert runtime_rig.startup.enabled is False
    assert runtime_rig.runtime.settings.launch_at_login is False


@pytest.mark.asyncio
async def test_new_conversation_closes_pending_approval(runtime_rig: RuntimeRig) -> None:
    runtime_rig.backend.response = ProposalResponse(
        kind="proposal",
        action=ActionProposal(
            id=UUID("7ebba76d-55d3-4fad-b7b2-30c8235e1a4d"),
            kind=ActionKind.OPEN_URL,
            target="https://example.com",
            summary="Open Example",
        ),
    )
    runtime_rig.runtime.handle_press()
    await runtime_rig.runtime.handle_release()
    dialog = runtime_rig.runtime._approval_dialog
    assert dialog is not None

    runtime_rig.runtime._new_conversation()
    await runtime_rig.runtime._operation_task

    assert runtime_rig.runtime._approval_dialog is None
    assert runtime_rig.runtime.graph.coordinator.pending is None
    assert dialog.isHidden()


@pytest.mark.asyncio
async def test_release_applies_configured_transcript_retention(
    runtime_rig: RuntimeRig,
) -> None:
    runtime_rig.runtime.handle_press()

    await runtime_rig.runtime.handle_release()

    entries = HistoryStore(
        runtime_rig.runtime.paths.history,
        Retention.SEVEN_DAYS,
    ).read()
    assert [(entry.role, entry.text) for entry in entries] == [
        ("user", "hello"),
        ("assistant", "A safe response."),
    ]


@pytest.mark.asyncio
async def test_history_write_failure_does_not_break_voice_turn(runtime_rig: RuntimeRig) -> None:
    runtime_rig.runtime.history_store = FailingHistoryStore()
    runtime_rig.runtime.handle_press()

    await runtime_rig.runtime.handle_release()

    assert runtime_rig.runtime.graph.coordinator.last_error is None
    assert runtime_rig.runtime.window.response_text.toPlainText() == "A safe response."


@pytest.mark.asyncio
async def test_shutdown_stops_input_before_closing_providers(runtime_rig: RuntimeRig) -> None:
    await runtime_rig.runtime.start()

    await runtime_rig.runtime.shutdown()

    assert runtime_rig.events.index("hotkey.stop") < runtime_rig.events.index("recorder.close")
    assert runtime_rig.speaker.close_calls == 1
    assert runtime_rig.backend.close_calls == 1


@pytest.mark.asyncio
async def test_shutdown_continues_after_recorder_close_failure(runtime_rig: RuntimeRig) -> None:
    await runtime_rig.runtime.start()
    runtime_rig.recorder.close_error = RuntimeError("recorder close failed")

    with pytest.raises(RuntimeError, match="recorder close failed"):
        await runtime_rig.runtime.shutdown()

    assert runtime_rig.speaker.close_calls == 1
    assert runtime_rig.backend.close_calls == 1
    assert runtime_rig.runtime.guard.acquire() is True
    runtime_rig.runtime.guard.release()


@pytest.mark.asyncio
async def test_concurrent_shutdown_callers_share_cleanup(runtime_rig: RuntimeRig) -> None:
    await runtime_rig.runtime.start()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_speaker_close() -> None:
        started.set()
        await release.wait()
        runtime_rig.speaker.close_calls += 1

    runtime_rig.speaker.close = slow_speaker_close
    first = asyncio.create_task(runtime_rig.runtime.shutdown())
    await started.wait()
    second = asyncio.create_task(runtime_rig.runtime.shutdown())
    await asyncio.sleep(0)

    assert second.done() is False
    release.set()
    await asyncio.gather(first, second)
    assert runtime_rig.recorder.close_calls == 1
    assert runtime_rig.speaker.close_calls == 1
    assert runtime_rig.backend.close_calls == 1


@pytest.mark.asyncio
async def test_hotkey_input_is_ignored_during_shutdown(runtime_rig: RuntimeRig) -> None:
    await runtime_rig.runtime.start()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_speaker_close() -> None:
        started.set()
        await release.wait()

    runtime_rig.speaker.close = slow_speaker_close
    shutdown = asyncio.create_task(runtime_rig.runtime.shutdown())
    await started.wait()

    runtime_rig.runtime.handle_press()
    await runtime_rig.runtime.handle_release()
    runtime_rig.runtime._new_conversation()

    assert runtime_rig.recorder.start_calls == 0
    assert runtime_rig.backend.reset_calls == 0
    release.set()
    await shutdown


@pytest.mark.asyncio
async def test_cancelled_shutdown_waiter_does_not_cancel_cleanup(
    runtime_rig: RuntimeRig,
) -> None:
    await runtime_rig.runtime.start()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_speaker_close() -> None:
        started.set()
        await release.wait()

    runtime_rig.speaker.close = slow_speaker_close
    waiter = asyncio.create_task(runtime_rig.runtime.shutdown())
    await started.wait()

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert runtime_rig.runtime._shutdown_task is not None
    assert runtime_rig.runtime._shutdown_task.cancelled() is False
    release.set()
    await runtime_rig.runtime.shutdown()
    assert runtime_rig.backend.close_calls == 1
