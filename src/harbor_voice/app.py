"""Qt application composition and Windows tray lifecycle."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import qasync
from platformdirs import user_data_path
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QDialog

from harbor_voice.actions import (
    ActionRouter,
    ClipboardExecutor,
    OpenApplicationExecutor,
    OpenUrlExecutor,
    WorkspaceWriteExecutor,
)
from harbor_voice.audio.capture import MemoryRecorder
from harbor_voice.audio.hotkey import GlobalHoldKey
from harbor_voice.backends.codex import CodexBackend
from harbor_voice.backends.speech import SapiSpeaker
from harbor_voice.backends.transcription import FasterWhisperTranscriber
from harbor_voice.coordinator import TurnCoordinator
from harbor_voice.domain import ActionKind, AppState
from harbor_voice.policy import PermissionPolicy
from harbor_voice.storage import AppPaths, AssistantSettings, SettingsStore, configure_logging
from harbor_voice.ui.tray import SingleInstanceGuard, TrayController
from harbor_voice.ui.window import ApprovalDialog, ConversationWindow, SettingsDialog


@dataclass(slots=True)
class ProviderBundle:
    transcriber: Any
    backend: Any
    speaker: Any
    recorder: Any
    runner: Any


@dataclass(slots=True)
class ComponentGraph:
    policy: PermissionPolicy
    coordinator: TurnCoordinator
    providers: ProviderBundle


def build_components(
    settings: AssistantSettings,
    providers: ProviderBundle,
    *,
    state_sink,
) -> ComponentGraph:
    if settings.workspace is None:
        raise ValueError("a working folder must be selected")
    policy = PermissionPolicy(settings.workspace, settings.registered_apps)
    coordinator = TurnCoordinator(
        transcriber=providers.transcriber,
        backend=providers.backend,
        speaker=providers.speaker,
        runner=providers.runner,
        policy=policy,
        state_sink=state_sink,
    )
    return ComponentGraph(policy, coordinator, providers)


class QtClipboard:
    def __init__(self, application: QApplication) -> None:
        self._clipboard = application.clipboard()

    def get_text(self) -> str:
        return self._clipboard.text()

    def set_text(self, text: str) -> None:
        self._clipboard.setText(text)


def default_providers(settings: AssistantSettings, application: QApplication) -> ProviderBundle:
    backend = CodexBackend()
    clipboard = ClipboardExecutor(QtClipboard(application))
    runner = ActionRouter(
        {
            ActionKind.FILE_WRITE: WorkspaceWriteExecutor(backend),
            ActionKind.OPEN_APP: OpenApplicationExecutor(settings.registered_apps),
            ActionKind.OPEN_URL: OpenUrlExecutor(),
            ActionKind.CLIPBOARD_READ: clipboard,
            ActionKind.CLIPBOARD_REPLACE: clipboard,
        }
    )
    return ProviderBundle(
        transcriber=FasterWhisperTranscriber(
            model_name=settings.stt_model,
            device=settings.stt_device,
            compute_type=settings.stt_compute_type,
        ),
        backend=backend,
        speaker=SapiSpeaker(),
        recorder=MemoryRecorder(device=settings.microphone_device),
        runner=runner,
    )


class _HotkeyBridge(QObject):
    pressed = Signal()
    released = Signal()


class HarborRuntime:
    def __init__(
        self,
        *,
        application: QApplication,
        loop: asyncio.AbstractEventLoop,
        paths: AppPaths,
        settings: AssistantSettings,
        settings_store: SettingsStore,
        providers: ProviderBundle,
        hotkey_factory=GlobalHoldKey,
        tray: TrayController | None = None,
        window: ConversationWindow | None = None,
        guard: SingleInstanceGuard | None = None,
    ) -> None:
        self.application = application
        self.loop = loop
        self.paths = paths
        self.settings = settings
        self.settings_store = settings_store
        self.providers = providers
        self.tray = tray or TrayController()
        self.window = window or ConversationWindow()
        self.guard = guard or SingleInstanceGuard(paths.root / "instance.lock")
        self.graph = build_components(settings, providers, state_sink=self._set_state)
        self._bridge = _HotkeyBridge()
        self._hotkey = hotkey_factory(
            settings.ptt_key,
            lambda _: self._bridge.pressed.emit(),
            lambda _: self._bridge.released.emit(),
        )
        self._approval_dialog: ApprovalDialog | None = None
        self._muted = False
        self._started = False
        self._closed = False
        self._wire_ui()

    async def start(self) -> bool:
        if not self.guard.acquire():
            return False
        await self.providers.backend.start(self.settings.workspace)
        self._hotkey.start()
        self.tray.show()
        self._set_state(AppState.IDLE)
        self._started = True
        return True

    def handle_press(self) -> None:
        if self._muted:
            return
        self.graph.coordinator.cancel_speech()
        self.providers.recorder.start()
        self._set_state(AppState.LISTENING)

    def handle_release(self) -> asyncio.Task[None]:
        recording = self.providers.recorder.stop()
        loop = self._running_loop()
        if recording is None:
            self._set_state(AppState.IDLE)

            async def nothing() -> None:
                return None

            return loop.create_task(nothing())
        return loop.create_task(self._submit(recording))

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._hotkey.stop()
        self.providers.recorder.close()
        await self.providers.speaker.close()
        await self.providers.backend.close()
        self.tray.hide()
        self.guard.release()

    async def _submit(self, recording) -> None:
        await self.graph.coordinator.submit(recording)
        self.window.set_turn(
            self.graph.coordinator.last_transcript,
            self.graph.coordinator.last_response,
        )

    def _set_state(self, state: AppState) -> None:
        self.tray.set_state(state)
        self.window.set_state(state)
        if state is AppState.APPROVAL and self.graph.coordinator.pending is not None:
            self._show_approval()

    def _show_approval(self) -> None:
        pending = self.graph.coordinator.pending
        if pending is None:
            return
        dialog = ApprovalDialog(pending.action, self.window)
        dialog.approved.connect(self._approve)
        dialog.rejected.connect(self._reject)
        self._approval_dialog = dialog
        dialog.show()
        dialog.raise_()

    def _approve(self, action_id) -> None:
        self._running_loop().create_task(self.graph.coordinator.approve(action_id))

    def _reject(self, action_id) -> None:
        self.graph.coordinator.reject(action_id)

    def _wire_ui(self) -> None:
        self._bridge.pressed.connect(self.handle_press)
        self._bridge.released.connect(self.handle_release)
        self.tray.show_requested.connect(self._show_window)
        self.tray.mute_requested.connect(self._toggle_mute)
        self.tray.settings_requested.connect(self._show_settings)
        self.tray.new_conversation_requested.connect(self._new_conversation)
        self.tray.quit_requested.connect(self._quit)

    def _show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self._set_state(AppState.MUTED if self._muted else AppState.IDLE)

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self.window)

        def save(settings: AssistantSettings) -> None:
            self.settings_store.save(settings)
            self.settings = settings
            self.tray.notify("Harbor Voice", "Settings saved. Restart to apply device changes.")

        dialog.saved.connect(save)
        dialog.show()

    def _new_conversation(self) -> None:
        self._running_loop().create_task(self.graph.coordinator.new_conversation())
        self.window.set_turn("", "")

    def _quit(self) -> None:
        async def finish() -> None:
            await self.shutdown()
            self.application.quit()

        self._running_loop().create_task(finish())

    def _running_loop(self) -> asyncio.AbstractEventLoop:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return self.loop


def _choose_initial_workspace(settings: AssistantSettings) -> AssistantSettings | None:
    if settings.workspace is not None:
        return settings
    dialog = SettingsDialog(settings)
    selected: list[AssistantSettings] = []
    dialog.saved.connect(selected.append)
    if dialog.exec() != QDialog.DialogCode.Accepted or not selected:
        return None
    return selected[0]


def main() -> int:
    application = QApplication(sys.argv)
    application.setQuitOnLastWindowClosed(False)
    paths = AppPaths.from_root(Path(user_data_path("HarborVoice")))
    settings_store = SettingsStore(paths.settings)
    settings = _choose_initial_workspace(settings_store.load())
    if settings is None:
        return 1
    settings_store.save(settings)
    configure_logging(paths)
    loop = qasync.QEventLoop(application)
    asyncio.set_event_loop(loop)
    runtime = HarborRuntime(
        application=application,
        loop=loop,
        paths=paths,
        settings=settings,
        settings_store=settings_store,
        providers=default_providers(settings, application),
    )
    with loop:
        started = loop.run_until_complete(runtime.start())
        if not started:
            return 2
        loop.run_forever()
        loop.run_until_complete(runtime.shutdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

