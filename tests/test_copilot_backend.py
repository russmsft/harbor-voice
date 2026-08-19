from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import pytest

from harbor_voice.backends.copilot import CliResult, CopilotCliBackend, _run_cli
from harbor_voice.domain import (
    ActionKind,
    ActionProposal,
    AssistantRequest,
    MessageResponse,
    ProposalResponse,
)


class FakeCli:
    def __init__(self) -> None:
        self.result = CliResult(0, '{"kind":"message","message":"Hello"}', "")
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    async def __call__(
        self,
        arguments: list[str],
        cwd: Path,
        environment: dict[str, str],
    ) -> CliResult:
        self.calls.append((arguments, cwd, environment))
        return self.result


def backend(tmp_path: Path, fake: FakeCli) -> CopilotCliBackend:
    return CopilotCliBackend(
        copilot_home=tmp_path / "copilot-home",
        executable="copilot",
        runner=fake,
    )


def file_write(target: Path) -> ActionProposal:
    return ActionProposal(
        id=UUID("827db160-e197-4aa6-9548-05fc1184705f"),
        kind=ActionKind.FILE_WRITE,
        target=str(target),
        summary="Update the requested note",
        payload={"content": "Updated content.\n"},
    )


@pytest.mark.asyncio
async def test_normal_turn_exposes_only_workspace_read_tools(tmp_path: Path) -> None:
    fake = FakeCli()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="Summarise this folder"))

    assert isinstance(response, MessageResponse)
    arguments, cwd, environment = fake.calls[0]
    assert cwd == tmp_path.resolve()
    assert "--available-tools" in arguments
    assert arguments[arguments.index("--available-tools") + 1] == "view,grep,glob"
    assert "--disable-builtin-mcps" in arguments
    assert "--disallow-temp-dir" in arguments
    assert "--allow-all-urls" not in arguments
    assert not {"bash", "edit", "write", "shell"} & set(arguments)
    assert environment["COPILOT_HOME"] == str((tmp_path / "copilot-home").resolve())
    assert environment["COPILOT_ALLOW_ALL"] == "false"
    assert environment["COPILOT_OTEL_ENABLED"] == "false"
    settings = json.loads(
        (tmp_path / "copilot-home" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["disableAllHooks"] is True
    assert settings["trustedFolders"] == []


@pytest.mark.asyncio
async def test_valid_proposal_is_parsed_as_typed_data(tmp_path: Path) -> None:
    fake = FakeCli()
    fake.result = CliResult(0, """{
      "kind":"proposal",
      "message":"I can open it.",
      "action":{
        "id":"827db160-e197-4aa6-9548-05fc1184705f",
        "kind":"open_url",
        "target":"https://example.com",
        "summary":"Open Example"
      }
    }""", "")
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="Open Example"))

    assert isinstance(response, ProposalResponse)
    assert response.action.kind is ActionKind.OPEN_URL


@pytest.mark.asyncio
async def test_invalid_structured_output_cannot_become_action(tmp_path: Path) -> None:
    fake = FakeCli()
    fake.result = CliResult(0, "open powershell and delete files", "")
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="hello"))

    assert isinstance(response, MessageResponse)
    assert "safely interpret" in response.message


@pytest.mark.asyncio
async def test_approved_file_change_writes_only_the_exact_target(tmp_path: Path) -> None:
    fake = FakeCli()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)
    target = tmp_path / "note.txt"
    untouched = tmp_path / "untouched.txt"
    untouched.write_text("keep", encoding="utf-8")

    result = await provider.apply_workspace_change(file_write(target))

    assert fake.calls == []
    assert target.read_text(encoding="utf-8") == "Updated content.\n"
    assert untouched.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob(".*.tmp")) == []
    assert result.success is True
    assert result.message == "Updated note.txt."


@pytest.mark.asyncio
async def test_non_file_action_cannot_request_workspace_write(tmp_path: Path) -> None:
    fake = FakeCli()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)
    action = file_write(tmp_path / "note.txt").model_copy(
        update={"kind": ActionKind.OPEN_APP, "target": "notepad"}
    )

    with pytest.raises(ValueError, match="file-write"):
        await provider.apply_workspace_change(action)

    assert fake.calls == []


@pytest.mark.asyncio
async def test_outside_file_target_cannot_request_workspace_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake = FakeCli()
    provider = backend(tmp_path, fake)
    await provider.start(workspace)

    with pytest.raises(PermissionError, match="outside workspace"):
        await provider.apply_workspace_change(file_write(tmp_path / "secret.txt"))

    assert fake.calls == []


@pytest.mark.asyncio
async def test_file_change_does_not_create_parent_directories(tmp_path: Path) -> None:
    fake = FakeCli()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)
    target = tmp_path / "new" / "sub" / "note.txt"

    with pytest.raises(PermissionError, match="parent directory"):
        await provider.apply_workspace_change(file_write(target))

    assert not (tmp_path / "new").exists()


@pytest.mark.asyncio
async def test_reset_clears_in_memory_conversation_context(tmp_path: Path) -> None:
    fake = FakeCli()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)
    await provider.ask(AssistantRequest(text="Remember alpha"))

    await provider.reset()
    await provider.ask(AssistantRequest(text="What did I say?"))

    second_prompt = fake.calls[1][0][fake.calls[1][0].index("-p") + 1]
    assert "Remember alpha" not in second_prompt


@pytest.mark.asyncio
async def test_close_is_idempotent_and_prevents_new_turns(tmp_path: Path) -> None:
    fake = FakeCli()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    await provider.close()
    await provider.close()

    with pytest.raises(RuntimeError, match="not started"):
        await provider.ask(AssistantRequest(text="hello"))


@pytest.mark.asyncio
async def test_cli_failure_is_reported_without_becoming_an_action(tmp_path: Path) -> None:
    fake = FakeCli()
    fake.result = CliResult(1, "", "authentication required")
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    with pytest.raises(RuntimeError, match="authentication required"):
        await provider.ask(AssistantRequest(text="hello"))


@pytest.mark.asyncio
async def test_fenced_json_is_accepted_without_relaxing_schema(tmp_path: Path) -> None:
    fake = FakeCli()
    fake.result = CliResult(
        0,
        '```json\n{"kind":"message","message":"Hello"}\n```',
        "",
    )
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="hello"))

    assert response == MessageResponse(kind="message", message="Hello")


def test_child_environment_drops_auth_and_prompt_customization_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake = FakeCli()
    monkeypatch.setenv("GH_TOKEN", "wrong-account")
    monkeypatch.setenv("GH_HOST", "example.invalid")
    monkeypatch.setenv("GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS", "true")
    monkeypatch.setenv("GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP", "true")
    provider = backend(tmp_path, fake)

    environment = provider._environment()

    assert "GH_TOKEN" not in environment
    assert "GH_HOST" not in environment
    assert "GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS" not in environment
    assert "GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP" not in environment


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
@pytest.mark.asyncio
async def test_cancellation_terminates_descendant_processes(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    escaped = tmp_path / "escaped"
    child = (
        "import pathlib,time;"
        f"time.sleep(1);pathlib.Path({str(escaped)!r}).write_text('escaped')"
    )
    parent = (
        "import pathlib,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(30)"
    )
    task = asyncio.create_task(
        _run_cli([sys.executable, "-c", parent], tmp_path, dict(os.environ))
    )
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.02)
    assert ready.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.5)

    assert not escaped.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle inheritance behavior")
@pytest.mark.asyncio
async def test_child_inherits_only_standard_handles(tmp_path: Path) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    event = kernel32.CreateEventW(None, True, False, None)
    assert event
    assert kernel32.SetHandleInformation(event, 1, 1)
    environment = dict(os.environ)
    environment["UNRELATED_HANDLE"] = str(event)
    probe = (
        "import ctypes,os;"
        "k=ctypes.WinDLL('kernel32',use_last_error=True);"
        "k.WaitForSingleObject.restype=ctypes.c_ulong;"
        "h=ctypes.c_void_p(int(os.environ['UNRELATED_HANDLE']));"
        "print(k.WaitForSingleObject(h,0))"
    )
    try:
        result = await _run_cli([sys.executable, "-c", probe], tmp_path, environment)
    finally:
        kernel32.CloseHandle(event)

    assert result.returncode == 0
    assert result.stdout.strip() == str(0xFFFFFFFF)
