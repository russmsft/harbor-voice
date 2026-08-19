"""Qt application composition and Windows tray lifecycle."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from contextlib import suppress
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
from harbor_voice.backends.copilot import CopilotCliBackend
from harbor_voice.backends.speech import SapiSpeaker
from harbor_voice.backends.transcription import FasterWhisperTranscriber
from harbor_voice.coordinator import TurnCoordinator
from harbor_voice.domain import ActionKind, AppState
from harbor_voice.policy import PermissionPolicy
from harbor_voice.startup import StartupRegistration
from harbor_voice.storage import (
    AppPaths,
    AssistantSettings,
    HistoryEntry,
    HistoryStore,
    SettingsStore,
    configure_logging,
)
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


class SettingsApplyError(RuntimeError):
    """Settings and launch registration could not be kept consistent."""


def persist_settings(
    settings: AssistantSettings,
    settings_store: SettingsStore,
    startup_registration: StartupRegistration,
) -> None:
    previous_startup = startup_registration.configured_entry()
    startup_changed = startup_registration.set_enabled(settings.launch_at_login)
    try:
        settings_store.save(settings)
    except (OSError, RuntimeError) as exc:
        if startup_changed:
            try:
                startup_registration.restore(previous_startup)
            except (OSError, RuntimeError) as rollback_error:
                raise SettingsApplyError(
                    "settings save failed and launch-at-login rollback failed: "
                    f"{rollback_error}"
                ) from exc
        raise


def build_components(
    settings: AssistantSettings,
    providers: ProviderBundle,
    *,
    state_sink,
    history_sink=None,
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
        history_sink=history_sink,
    )
    return ComponentGraph(policy, coordinator, providers)


class QtClipboard:
    def __init__(self, application: QApplication) -> None:
        self._clipboard = application.clipboard()

    def get_text(self) -> str:
        return self._clipboard.text()

    def set_text(self, text: str) -> None:
        self._clipboard.setText(text)


def default_providers(
    settings: AssistantSettings,
    application: QApplication,
    *,
    copilot_home: Path,
) -> ProviderBundle:
    backend = CopilotCliBackend(copilot_home=copilot_home)
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
        startup_registration: StartupRegistration | None = None,
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
        self.startup_registration = startup_registration or StartupRegistration()
        self.history_store = HistoryStore(paths.history, settings.retention)
        self.graph = build_components(
            settings,
            providers,
            state_sink=self._set_state,
            history_sink=self._record_history,
        )
        self._bridge = _HotkeyBridge()
        self._hotkey = hotkey_factory(
            settings.ptt_key,
            lambda _: self._bridge.pressed.emit(),
            lambda _: self._bridge.released.emit(),
        )
        self._approval_dialog: ApprovalDialog | None = None
        self._operation_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._recording = False
        self._operation_state = AppState.IDLE
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
        if self._closed or self._muted or self._recording:
            return
        active = self._operation_task
        if active is not None and not active.done():
            if self.graph.coordinator.state is not AppState.SPEAKING:
                return
            active.cancel()
        elif self._operation_state not in {AppState.IDLE, AppState.ERROR}:
            return
        self.graph.coordinator.cancel_speech()
        try:
            self.providers.recorder.start()
        except Exception as exc:
            self.graph.coordinator.report_error(exc)
            return
        self._recording = True
        self._set_state(AppState.LISTENING)

    def handle_release(self) -> asyncio.Task[None]:
        loop = self._running_loop()
        if self._closed or not self._recording:
            return loop.create_task(self._nothing())
        self._recording = False
        try:
            recording = self.providers.recorder.stop()
        except Exception as exc:
            self.graph.coordinator.report_error(exc)
            return loop.create_task(self._nothing())
        if recording is None:
            self._set_state(AppState.IDLE)
            return loop.create_task(self._nothing())
        previous = self._operation_task
        return self._start_operation(self._submit_after(previous, recording))

    async def shutdown(self) -> None:
        task = self._shutdown_task
        if task is None:
            self._closed = True
            self._recording = False
            task = self._running_loop().create_task(self._shutdown())
            self._shutdown_task = task
        await asyncio.shield(task)

    async def _shutdown(self) -> None:
        errors: list[Exception] = []
        dialog = self._approval_dialog
        if dialog is not None:
            dialog.reject()
        try:
            self._hotkey.stop()
        except Exception as exc:
            errors.append(exc)
        try:
            self.providers.recorder.close()
        except Exception as exc:
            errors.append(exc)
        active = self._operation_task
        if active is not None and not active.done():
            active.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await active
            except Exception as exc:
                errors.append(exc)
        try:
            await self.providers.speaker.close()
        except Exception as exc:
            errors.append(exc)
        try:
            await self.providers.backend.close()
        except Exception as exc:
            errors.append(exc)
        try:
            self.tray.hide()
        except Exception as exc:
            errors.append(exc)
        try:
            self.guard.release()
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise RuntimeError(
                f"shutdown completed with {len(errors)} cleanup error(s): {errors[0]}"
            ) from errors[0]

    async def _submit(self, recording) -> None:
        await self.graph.coordinator.submit(recording)
        self.window.set_turn(
            self.graph.coordinator.last_transcript,
            self.graph.coordinator.last_response,
        )

    def _record_history(self, role: str, text: str) -> None:
        try:
            self.history_store.append(HistoryEntry(role=role, text=text))
        except OSError:
            self.tray.notify(
                "Harbor Voice",
                "This turn could not be added to transcript history.",
            )

    def _set_state(self, state: AppState) -> None:
        self._operation_state = state
        self._render_state()
        if state is AppState.APPROVAL and self.graph.coordinator.pending is not None:
            self._show_approval()
        elif state is AppState.ERROR and self.graph.coordinator.last_error:
            self.window.set_error(self.graph.coordinator.last_error)

    def _show_approval(self) -> None:
        pending = self.graph.coordinator.pending
        if pending is None:
            return
        dialog = ApprovalDialog(
            pending.action,
            self.window,
            display_target=pending.display_target,
        )
        dialog.action_approved.connect(self._approve)
        dialog.action_rejected.connect(self._reject)
        dialog.action_expired.connect(self._expire)
        self._approval_dialog = dialog
        dialog.show()
        dialog.raise_()

    def _approve(self, action_id) -> None:
        if self._closed:
            self._reject(action_id)
            return
        self._forget_approval_dialog(action_id)
        self._start_operation(self.graph.coordinator.approve(action_id))

    def _reject(self, action_id) -> None:
        self._forget_approval_dialog(action_id)
        pending = self.graph.coordinator.pending
        if pending is not None and pending.action.id == action_id:
            self.graph.coordinator.reject(action_id)

    def _expire(self, action_id) -> None:
        self._reject(action_id)
        self.tray.notify("Harbor Voice", "The pending approval expired.")

    def _forget_approval_dialog(self, action_id) -> None:
        dialog = self._approval_dialog
        if dialog is not None and dialog.action_id == action_id:
            self._approval_dialog = None

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
        self._render_state()

    def _show_settings(self) -> None:
        if self._closed:
            return
        dialog = SettingsDialog(self.settings, self.window)
        dialog.saved.connect(self._save_settings)
        dialog.show()

    def _save_settings(self, settings: AssistantSettings) -> None:
        if self._closed:
            return
        try:
            persist_settings(settings, self.settings_store, self.startup_registration)
        except (OSError, RuntimeError) as exc:
            self.tray.notify("Harbor Voice", f"Settings could not be saved: {exc}")
            return
        self.settings = settings
        self.tray.notify(
            "Harbor Voice",
            "Settings saved. Restart to apply audio, shortcut, or workspace changes.",
        )

    def _new_conversation(self) -> None:
        if self._closed:
            return
        dialog = self._approval_dialog
        if dialog is not None:
            dialog.reject()
        active = self._operation_task
        if active is not None and not active.done():
            self.graph.coordinator.cancel_speech()
            active.cancel()
        self._start_operation(self._new_conversation_after(active))
        self.window.set_turn("", "")

    def _quit(self) -> None:
        async def finish() -> None:
            try:
                await self.shutdown()
            except Exception as exc:
                self.graph.coordinator.report_error(exc)
            finally:
                self.application.quit()

        self._running_loop().create_task(finish())

    def _running_loop(self) -> asyncio.AbstractEventLoop:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return self.loop

    def _start_operation(self, operation: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = self._running_loop().create_task(operation)
        self._operation_task = task
        task.add_done_callback(self._operation_finished)
        return task

    def _operation_finished(self, task: asyncio.Task[None]) -> None:
        if self._operation_task is task:
            self._operation_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.graph.coordinator.report_error(error)

    def _render_state(self) -> None:
        visible = AppState.MUTED if self._muted else self._operation_state
        self.tray.set_state(visible)
        self.window.set_state(visible)

    async def _submit_after(
        self,
        previous: asyncio.Task[None] | None,
        recording,
    ) -> None:
        await self._finish_previous(previous)
        await self._submit(recording)

    async def _new_conversation_after(self, previous: asyncio.Task[None] | None) -> None:
        await self._finish_previous(previous)
        await self.graph.coordinator.new_conversation()

    @staticmethod
    async def _finish_previous(previous: asyncio.Task[None] | None) -> None:
        if previous is None or previous.done():
            return
        with suppress(asyncio.CancelledError):
            await previous

    @staticmethod
    async def _nothing() -> None:
        return None


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
    paths.ensure_owned_root()
    settings_store = SettingsStore(paths.settings)
    startup_registration = StartupRegistration()
    settings = settings_store.load()
    with suppress(OSError):
        settings = settings.model_copy(
            update={"launch_at_login": startup_registration.is_enabled()}
        )
    settings = _choose_initial_workspace(settings)
    if settings is None:
        return 1
    try:
        persist_settings(settings, settings_store, startup_registration)
    except (OSError, RuntimeError) as exc:
        print(f"Launch-at-login could not be configured: {exc}", file=sys.stderr)
        return 1
    configure_logging(paths)
    loop = qasync.QEventLoop(application)
    asyncio.set_event_loop(loop)
    runtime = HarborRuntime(
        application=application,
        loop=loop,
        paths=paths,
        settings=settings,
        settings_store=settings_store,
        providers=default_providers(
            settings,
            application,
            copilot_home=paths.root / "copilot-cli",
        ),
        startup_registration=startup_registration,
    )
    with loop:
        started = loop.run_until_complete(runtime.start())
        if not started:
            return 2
        loop.run_forever()
        try:
            loop.run_until_complete(runtime.shutdown())
        except RuntimeError as exc:
            print(f"Harbor Voice shutdown completed with errors: {exc}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
