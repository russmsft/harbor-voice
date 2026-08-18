"""Typed local effects. There is intentionally no generic command executor."""

from __future__ import annotations

import subprocess
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from harbor_voice.domain import ActionKind, ActionProposal, ActionResult


class UnsupportedAction(RuntimeError):
    """An action has no safe executor for its exact type and target."""


class Executor(Protocol):
    async def execute(self, action: ActionProposal) -> ActionResult: ...


class ClipboardPort(Protocol):
    def get_text(self) -> str: ...

    def set_text(self, text: str) -> None: ...


class WorkspaceBackend(Protocol):
    async def apply_workspace_change(self, action: ActionProposal) -> ActionResult: ...


class OpenApplicationExecutor:
    def __init__(
        self,
        registered_apps: dict[str, Path],
        *,
        start: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self._registered = {
            name.casefold(): path.expanduser().resolve(strict=False)
            for name, path in registered_apps.items()
        }
        self._start = start

    async def execute(self, action: ActionProposal) -> ActionResult:
        if action.kind is not ActionKind.OPEN_APP:
            raise UnsupportedAction("application executor received the wrong action kind")
        executable = self._registered.get(action.target.casefold())
        if executable is None:
            raise UnsupportedAction(f"application is not registered: {action.target}")
        if not executable.is_file():
            raise UnsupportedAction(f"registered application is missing: {executable}")
        self._start([str(executable)], shell=False)
        return ActionResult(success=True, message=f"Opened {action.target}.")


class OpenUrlExecutor:
    def __init__(self, *, opener: Callable[[str], Any] = webbrowser.open_new_tab) -> None:
        self._opener = opener

    async def execute(self, action: ActionProposal) -> ActionResult:
        if action.kind is not ActionKind.OPEN_URL:
            raise UnsupportedAction("URL executor received the wrong action kind")
        parsed = urlsplit(action.target)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            raise UnsupportedAction("only HTTPS URLs can be opened")
        if parsed.username or parsed.password:
            raise UnsupportedAction("HTTPS URLs containing credentials are blocked")
        opened = self._opener(action.target)
        if opened is False:
            raise RuntimeError("Windows did not accept the URL launch request")
        return ActionResult(success=True, message=f"Opened {parsed.hostname}.")


class ClipboardExecutor:
    def __init__(self, clipboard: ClipboardPort) -> None:
        self._clipboard = clipboard

    async def execute(self, action: ActionProposal) -> ActionResult:
        if action.kind is ActionKind.CLIPBOARD_READ:
            text = self._clipboard.get_text()
            preview = text[:500]
            message = "The clipboard is empty." if not text else f"Clipboard text: {preview}"
            return ActionResult(success=True, message=message, details={"text": text})
        if action.kind is ActionKind.CLIPBOARD_REPLACE:
            text = action.payload.get("text")
            if text is None:
                raise UnsupportedAction("clipboard replacement text is missing")
            self._clipboard.set_text(text)
            return ActionResult(success=True, message="Replaced the clipboard text.")
        raise UnsupportedAction("clipboard executor received the wrong action kind")


class WorkspaceWriteExecutor:
    def __init__(self, backend: WorkspaceBackend) -> None:
        self._backend = backend

    async def execute(self, action: ActionProposal) -> ActionResult:
        if action.kind is not ActionKind.FILE_WRITE:
            raise UnsupportedAction("workspace executor accepts file-write actions only")
        return await self._backend.apply_workspace_change(action)


class ActionRouter:
    def __init__(self, executors: dict[ActionKind, Executor]) -> None:
        self._executors = dict(executors)

    async def execute(self, action: ActionProposal) -> ActionResult:
        executor = self._executors.get(action.kind)
        if executor is None:
            raise UnsupportedAction(f"no executor is registered for {action.kind.value}")
        return await executor.execute(action)

