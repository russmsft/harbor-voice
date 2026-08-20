from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import pytest

from harbor_voice.backends.copilot import (
    CopilotAcpConnection,
    CopilotCliBackend,
    _run_cli,
)
from harbor_voice.domain import (
    ActionKind,
    ActionProposal,
    AssistantRequest,
    MessageResponse,
    ProposalResponse,
)


class FakeAcpConnection:
    def __init__(self) -> None:
        self.response = '{"kind":"message","message":"Hello"}'
        self.error: Exception | None = None
        self.started = False
        self.closed = False
        self.block_start = False
        self.start_release = asyncio.Event()
        self.sessions: list[Path] = []
        self.new_session_calls = 0
        self.new_session_error_at: int | None = None
        self.prompts: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.cancel_error: Exception | None = None
        self.block_prompts = False
        self.prompt_started = asyncio.Event()
        self.prompt_release = asyncio.Event()

    async def start(self) -> None:
        self.started = True
        if self.block_start:
            await self.start_release.wait()

    async def new_session(self, workspace: Path) -> str:
        self.new_session_calls += 1
        if self.new_session_error_at == self.new_session_calls:
            raise RuntimeError("new session failed")
        self.sessions.append(workspace)
        return f"session-{len(self.sessions)}"

    async def prompt(self, session_id: str, text: str) -> str:
        self.prompts.append((session_id, text))
        self.prompt_started.set()
        if self.block_prompts:
            await self.prompt_release.wait()
        if self.error is not None:
            raise self.error
        return self.response

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        self.prompt_release.set()

    async def close(self) -> None:
        self.closed = True

    async def abort(self) -> None:
        self.closed = True
        self.prompt_release.set()


class FakeAcpFactory:
    def __init__(self) -> None:
        self.connection = FakeAcpConnection()
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(
        self,
        arguments: list[str],
        cwd: Path,
        environment: dict[str, str],
    ) -> FakeAcpConnection:
        self.calls.append((arguments, cwd, environment))
        return self.connection


class ScriptedAcpProcess:
    def __init__(self) -> None:
        self.writes: list[dict] = []
        self.responses: asyncio.Queue[str] = asyncio.Queue()
        self.closed = asyncio.Event()
        self.terminated = False

    async def write_line(self, line: str) -> None:
        message = json.loads(line)
        self.writes.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            await self._respond(request_id, {"protocolVersion": 1})
        elif method == "session/new":
            await self._respond(request_id, {"sessionId": "session-1"})
        elif method == "session/prompt":
            await self.responses.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 99,
                        "method": "session/request_permission",
                        "params": {
                            "options": [
                                {"optionId": "allow", "kind": "allow_once"},
                                {"optionId": "reject", "kind": "reject_once"},
                            ]
                        },
                    }
                )
                + "\n"
            )
            await self.responses.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": "Working on it.",
                                },
                            }
                        },
                    }
                )
                + "\n"
            )
            await self.responses.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "tool_call",
                                "toolCallId": "tool-1",
                                "title": "Read a file",
                                "kind": "read",
                                "status": "completed",
                            }
                        },
                    }
                )
                + "\n"
            )
            await self.responses.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": '{"kind":"message",',
                                },
                            }
                        },
                    }
                )
                + "\n"
            )
            await self.responses.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": '"message":"Hello"}',
                                },
                            }
                        },
                    }
                )
                + "\n"
            )
            await self._respond(request_id, {"stopReason": "end_turn"})

    async def _respond(self, request_id: int, result: dict) -> None:
        await self.responses.put(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n"
        )

    async def read_stdout_line(self) -> str:
        return await self.responses.get()

    async def read_stderr(self) -> str:
        await self.closed.wait()
        return ""

    async def close_stdin(self) -> None:
        self.closed.set()

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.closed.set()

    def close(self) -> None:
        self.closed.set()


def backend(tmp_path: Path, fake: FakeAcpFactory) -> CopilotCliBackend:
    return CopilotCliBackend(
        copilot_home=tmp_path / "copilot-home",
        executable="copilot",
        connection_factory=fake,
    )


@pytest.mark.asyncio
async def test_acp_connection_reuses_session_and_denies_permission_requests(
    tmp_path: Path,
) -> None:
    process = ScriptedAcpProcess()

    async def process_factory(arguments, cwd, environment):
        del arguments, cwd, environment
        return process

    connection = CopilotAcpConnection(
        ["copilot", "--acp", "--stdio"],
        tmp_path,
        {},
        process_factory=process_factory,
    )

    await connection.start()
    session_id = await connection.new_session(tmp_path)
    response = await connection.prompt(session_id, "hello")
    await connection.close()

    assert response == '{"kind":"message","message":"Hello"}'
    assert [
        message["method"]
        for message in process.writes
        if "method" in message
    ] == [
        "initialize",
        "session/new",
        "session/prompt",
    ]
    permission_response = next(
        message for message in process.writes if message.get("id") == 99
    )
    assert permission_response["result"] == {
        "outcome": {"outcome": "selected", "optionId": "reject"}
    }


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
    fake = FakeAcpFactory()
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
    assert arguments[:3] == ["copilot", "--acp", "--stdio"]
    assert arguments[arguments.index("--effort") + 1] == "low"
    assert arguments[arguments.index("--allow-tool") + 1] == "view,grep,glob"
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
async def test_acp_startup_timeout_aborts_connection(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    fake.connection.block_start = True
    provider = CopilotCliBackend(
        copilot_home=tmp_path / "copilot-home",
        executable="copilot",
        connection_factory=fake,
        startup_timeout_seconds=0.01,
    )

    with pytest.raises(TimeoutError):
        await provider.start(tmp_path)

    assert fake.connection.closed is True
    assert provider.workspace is None


@pytest.mark.asyncio
async def test_valid_proposal_is_parsed_as_typed_data(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    fake.connection.response = """{
      "kind":"proposal",
      "message":"I can open it.",
      "action":{
        "id":"827db160-e197-4aa6-9548-05fc1184705f",
        "kind":"open_url",
        "target":"https://example.com",
        "summary":"Open Example"
      }
    }"""
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="Open Example"))

    assert isinstance(response, ProposalResponse)
    assert response.action.kind is ActionKind.OPEN_URL


@pytest.mark.asyncio
async def test_invalid_structured_output_cannot_become_action(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    fake.connection.response = "open powershell and delete files"
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="hello"))

    assert isinstance(response, MessageResponse)
    assert "safely interpret" in response.message


@pytest.mark.asyncio
async def test_approved_file_change_writes_only_the_exact_target(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)
    target = tmp_path / "note.txt"
    untouched = tmp_path / "untouched.txt"
    untouched.write_text("keep", encoding="utf-8")

    result = await provider.apply_workspace_change(file_write(target))

    assert fake.connection.prompts == []
    assert target.read_text(encoding="utf-8") == "Updated content.\n"
    assert untouched.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob(".*.tmp")) == []
    assert result.success is True
    assert result.message == "Updated note.txt."


@pytest.mark.asyncio
async def test_non_file_action_cannot_request_workspace_write(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)
    action = file_write(tmp_path / "note.txt").model_copy(
        update={"kind": ActionKind.OPEN_APP, "target": "notepad"}
    )

    with pytest.raises(ValueError, match="file-write"):
        await provider.apply_workspace_change(action)

    assert fake.connection.prompts == []


@pytest.mark.asyncio
async def test_outside_file_target_cannot_request_workspace_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake = FakeAcpFactory()
    provider = backend(tmp_path, fake)
    await provider.start(workspace)

    with pytest.raises(PermissionError, match="outside workspace"):
        await provider.apply_workspace_change(file_write(tmp_path / "secret.txt"))

    assert fake.connection.prompts == []


@pytest.mark.asyncio
async def test_file_change_does_not_create_parent_directories(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)
    target = tmp_path / "new" / "sub" / "note.txt"

    with pytest.raises(PermissionError, match="parent directory"):
        await provider.apply_workspace_change(file_write(target))

    assert not (tmp_path / "new").exists()


@pytest.mark.asyncio
async def test_reset_clears_in_memory_conversation_context(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)
    await provider.ask(AssistantRequest(text="Remember alpha"))

    await provider.reset()
    await provider.ask(AssistantRequest(text="What did I say?"))

    assert fake.connection.sessions == [tmp_path.resolve(), tmp_path.resolve()]
    second_prompt = fake.connection.prompts[1][1]
    assert "Remember alpha" not in second_prompt


@pytest.mark.asyncio
async def test_close_is_idempotent_and_prevents_new_turns(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    await provider.close()
    await provider.close()

    assert fake.connection.closed is True
    with pytest.raises(RuntimeError, match="not started"):
        await provider.ask(AssistantRequest(text="hello"))


@pytest.mark.asyncio
async def test_cli_failure_is_reported_without_becoming_an_action(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    fake.connection.error = RuntimeError("authentication required")
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    with pytest.raises(RuntimeError, match="authentication required"):
        await provider.ask(AssistantRequest(text="hello"))


@pytest.mark.asyncio
async def test_cancelled_turn_cancels_and_drains_persistent_session(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    fake.connection.block_prompts = True
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)
    task = asyncio.create_task(provider.ask(AssistantRequest(text="hello")))
    await fake.connection.prompt_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake.connection.cancelled == ["session-1"]
    fake.connection.block_prompts = False
    response = await provider.ask(AssistantRequest(text="fresh"))
    assert response == MessageResponse(kind="message", message="Hello")


@pytest.mark.asyncio
async def test_failed_cancellation_restarts_persistent_connection(tmp_path: Path) -> None:
    first = FakeAcpConnection()
    first.block_prompts = True
    first.cancel_error = RuntimeError("cancel failed")
    second = FakeAcpConnection()
    connections = iter([first, second])

    def connection_factory(arguments, cwd, environment):
        del arguments, cwd, environment
        return next(connections)

    provider = CopilotCliBackend(
        copilot_home=tmp_path / "copilot-home",
        executable="copilot",
        connection_factory=connection_factory,
    )
    await provider.start(tmp_path)
    task = asyncio.create_task(provider.ask(AssistantRequest(text="hello")))
    await first.prompt_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert first.closed is True
    assert second.started is False
    await provider.reset()
    assert second.started is True
    response = await provider.ask(AssistantRequest(text="fresh"))
    assert response == MessageResponse(kind="message", message="Hello")


@pytest.mark.asyncio
async def test_transport_failure_restarts_and_retries_current_turn(tmp_path: Path) -> None:
    first = FakeAcpConnection()
    second = FakeAcpConnection()
    connections = iter([first, second])

    def connection_factory(arguments, cwd, environment):
        del arguments, cwd, environment
        return next(connections)

    provider = CopilotCliBackend(
        copilot_home=tmp_path / "copilot-home",
        executable="copilot",
        connection_factory=connection_factory,
    )
    await provider.start(tmp_path)
    await provider.ask(AssistantRequest(text="Remember alpha"))
    first.error = RuntimeError("ACP closed unexpectedly")

    response = await provider.ask(AssistantRequest(text="What did I say?"))

    assert first.closed is True
    assert second.started is True
    assert response == MessageResponse(kind="message", message="Hello")
    assert "Remember alpha" in second.prompts[0][1]


@pytest.mark.asyncio
async def test_failed_reset_reopens_blank_session(tmp_path: Path) -> None:
    first = FakeAcpConnection()
    first.new_session_error_at = 2
    second = FakeAcpConnection()
    connections = iter([first, second])

    def connection_factory(arguments, cwd, environment):
        del arguments, cwd, environment
        return next(connections)

    provider = CopilotCliBackend(
        copilot_home=tmp_path / "copilot-home",
        executable="copilot",
        connection_factory=connection_factory,
    )
    await provider.start(tmp_path)
    await provider.ask(AssistantRequest(text="Remember alpha"))

    await provider.reset()
    response = await provider.ask(AssistantRequest(text="What did I say?"))

    assert first.closed is True
    assert second.started is True
    assert response == MessageResponse(kind="message", message="Hello")
    assert "Remember alpha" not in second.prompts[0][1]


@pytest.mark.asyncio
async def test_fenced_json_is_accepted_without_relaxing_schema(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    fake.connection.response = '```json\n{"kind":"message","message":"Hello"}\n```'
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="hello"))

    assert response == MessageResponse(kind="message", message="Hello")


@pytest.mark.asyncio
async def test_acp_progress_before_final_json_is_ignored(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    fake.connection.response = (
        "I'll answer briefly without taking any action.\n"
        '{"kind":"message","message":"Hello"}'
    )
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="hello"))

    assert response == MessageResponse(kind="message", message="Hello")


@pytest.mark.asyncio
async def test_acp_progress_cannot_promote_a_trailing_action(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    fake.connection.response = (
        "I will propose an action.\n"
        '{"kind":"proposal","action":{'
        '"id":"827db160-e197-4aa6-9548-05fc1184705f",'
        '"kind":"clipboard_read","target":"clipboard","summary":"Read clipboard"}}'
    )
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="read clipboard"))

    assert isinstance(response, MessageResponse)
    assert "safely interpret" in response.message


@pytest.mark.asyncio
async def test_jsonl_output_uses_unwrapped_assistant_content(tmp_path: Path) -> None:
    fake = FakeAcpFactory()
    content = json.dumps(
        {
            "kind": "message",
            "message": (
                "This long response remains valid because JSONL carries the assistant "
                "content without terminal line wrapping inside its JSON string."
            ),
        }
    )
    fake.connection.response = "\n".join(
        [
            json.dumps({"type": "assistant.turn_start", "data": {"turnId": "1"}}),
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {"content": content},
                }
            ),
            json.dumps({"type": "result", "data": {}}),
        ]
    )
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="hello"))

    assert isinstance(response, MessageResponse)
    assert response.message.startswith("This long response remains valid")


@pytest.mark.parametrize(
    "stdout",
    [
        "null",
        "[]",
        '"hello"',
        '{"type":"assistant.message","data":null}',
    ],
)
@pytest.mark.asyncio
async def test_malformed_jsonl_cannot_bypass_safe_parse_failure(
    tmp_path: Path,
    stdout: str,
) -> None:
    fake = FakeAcpFactory()
    fake.connection.response = stdout
    provider = backend(tmp_path, fake)
    await provider.start(tmp_path)

    response = await provider.ask(AssistantRequest(text="hello"))

    assert isinstance(response, MessageResponse)
    assert "safely interpret" in response.message


def test_child_environment_drops_auth_and_prompt_customization_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake = FakeAcpFactory()
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
