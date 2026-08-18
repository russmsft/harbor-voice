from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from harbor_voice.actions import (
    ActionRouter,
    ClipboardExecutor,
    OpenApplicationExecutor,
    OpenUrlExecutor,
    UnsupportedAction,
    WorkspaceWriteExecutor,
)
from harbor_voice.domain import ActionKind, ActionProposal, ActionResult


def action(
    kind: ActionKind,
    target: str,
    *,
    payload: dict[str, str] | None = None,
) -> ActionProposal:
    return ActionProposal(
        id=UUID("d0b74c8b-1bf0-4bb8-9fd0-60202bf96922"),
        kind=kind,
        target=target,
        summary="Perform requested action",
        payload=payload or {},
    )


@pytest.mark.asyncio
async def test_app_executor_uses_registered_path_without_shell(tmp_path: Path) -> None:
    executable = tmp_path / "notepad.exe"
    executable.write_bytes(b"MZ")
    calls: list[tuple[list[str], dict]] = []

    def start(args: list[str], **kwargs) -> None:
        calls.append((args, kwargs))

    executor = OpenApplicationExecutor({"Notepad": executable}, start=start)

    result = await executor.execute(action(ActionKind.OPEN_APP, "notepad"))

    assert calls == [([str(executable.resolve())], {"shell": False})]
    assert result.success is True


@pytest.mark.asyncio
async def test_app_executor_rejects_unregistered_name(tmp_path: Path) -> None:
    executor = OpenApplicationExecutor({}, start=lambda *args, **kwargs: None)

    with pytest.raises(UnsupportedAction, match="not registered"):
        await executor.execute(action(ActionKind.OPEN_APP, "powershell"))


@pytest.mark.asyncio
async def test_url_executor_opens_only_https() -> None:
    opened: list[str] = []
    executor = OpenUrlExecutor(opener=lambda url: opened.append(url) or True)

    result = await executor.execute(action(ActionKind.OPEN_URL, "https://example.com/path"))

    assert opened == ["https://example.com/path"]
    assert result.success is True
    with pytest.raises(UnsupportedAction, match="HTTPS"):
        await executor.execute(action(ActionKind.OPEN_URL, "file:///C:/Windows/win.ini"))


class FakeClipboard:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.set_calls: list[str] = []

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text
        self.set_calls.append(text)


@pytest.mark.asyncio
async def test_clipboard_read_returns_text_without_mutation() -> None:
    clipboard = FakeClipboard("shopping list")
    executor = ClipboardExecutor(clipboard)

    result = await executor.execute(action(ActionKind.CLIPBOARD_READ, "clipboard"))

    assert result.details["text"] == "shopping list"
    assert clipboard.set_calls == []


@pytest.mark.asyncio
async def test_clipboard_replace_uses_validated_payload() -> None:
    clipboard = FakeClipboard()
    executor = ClipboardExecutor(clipboard)

    await executor.execute(
        action(ActionKind.CLIPBOARD_REPLACE, "clipboard", payload={"text": "new text"})
    )

    assert clipboard.set_calls == ["new text"]


class FakeWorkspaceBackend:
    def __init__(self) -> None:
        self.actions: list[ActionProposal] = []

    async def apply_workspace_change(self, proposal: ActionProposal) -> ActionResult:
        self.actions.append(proposal)
        return ActionResult(success=True, message="File updated.")


@pytest.mark.asyncio
async def test_workspace_executor_delegates_only_file_write(tmp_path: Path) -> None:
    backend = FakeWorkspaceBackend()
    executor = WorkspaceWriteExecutor(backend)
    proposal = action(ActionKind.FILE_WRITE, str(tmp_path / "note.txt"))

    result = await executor.execute(proposal)

    assert backend.actions == [proposal]
    assert result.message == "File updated."


@pytest.mark.asyncio
async def test_router_has_no_generic_fallback() -> None:
    router = ActionRouter({})

    with pytest.raises(UnsupportedAction, match="no executor"):
        await router.execute(action(ActionKind.OPEN_APP, "notepad"))


@pytest.mark.asyncio
async def test_router_dispatches_exact_action_kind() -> None:
    clipboard = FakeClipboard("hello")
    executor = ClipboardExecutor(clipboard)
    router = ActionRouter({ActionKind.CLIPBOARD_READ: executor})

    result = await router.execute(action(ActionKind.CLIPBOARD_READ, "clipboard"))

    assert result.details == {"text": "hello"}

