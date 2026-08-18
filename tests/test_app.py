from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from PySide6.QtWidgets import QApplication

from harbor_voice.app import HarborRuntime, ProviderBundle, build_components
from harbor_voice.domain import ActionResult, AppState, AudioRecording, MessageResponse, Transcript
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

    def start(self) -> None:
        self.events.append("recorder.start")
        self.start_calls += 1

    def stop(self) -> AudioRecording | None:
        self.events.append("recorder.stop")
        return self.recording

    def close(self) -> None:
        self.events.append("recorder.close")
        self.close_calls += 1


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


@dataclass
class RuntimeRig:
    runtime: HarborRuntime
    events: list[str]
    backend: StartableBackend
    speaker: FakeSpeaker
    recorder: FakeRecorder
    hotkey: FakeHotkey


@pytest_asyncio.fixture
async def runtime_rig(tmp_path: Path, qapp: QApplication) -> RuntimeRig:
    events: list[str] = []
    backend = StartableBackend(events)
    backend.response = MessageResponse(kind="message", message="A safe response.")
    speaker = FakeSpeaker(events)
    recorder = FakeRecorder(events)
    hotkey = FakeHotkey(events)
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
    )
    return RuntimeRig(runtime, events, backend, speaker, recorder, hotkey)


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
    assert runtime_rig.events.index("backend.start") < runtime_rig.events.index("hotkey.start")
    await runtime_rig.runtime.shutdown()


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
