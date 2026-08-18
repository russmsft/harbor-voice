from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from openai_codex import ApprovalMode, Sandbox

from harbor_voice.backends.codex import CodexBackend
from harbor_voice.domain import (
    ActionKind,
    ActionProposal,
    AssistantRequest,
    MessageResponse,
    ProposalResponse,
)


@dataclass
class RunCall:
    prompt: str
    kwargs: dict


class FakeThread:
    def __init__(self, response: str = '{"kind":"message","message":"Hello"}') -> None:
        self.final_response = response
        self.run_calls: list[RunCall] = []

    async def run(self, prompt: str, **kwargs):
        self.run_calls.append(RunCall(prompt, kwargs))
        return SimpleNamespace(final_response=self.final_response)


class FakeCodex:
    def __init__(self) -> None:
        self.thread = FakeThread()
        self.start_calls: list[dict] = []
        self.enter_calls = 0
        self.exit_calls = 0

    async def __aenter__(self):
        self.enter_calls += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exit_calls += 1

    async def thread_start(self, **kwargs) -> FakeThread:
        self.start_calls.append(kwargs)
        return self.thread


def file_write(target: Path) -> ActionProposal:
    return ActionProposal(
        id=UUID("827db160-e197-4aa6-9548-05fc1184705f"),
        kind=ActionKind.FILE_WRITE,
        target=str(target),
        summary="Update the requested note",
        payload={"content": "Updated content.\n"},
    )


@pytest.mark.asyncio
async def test_normal_turn_is_read_only_and_denies_sdk_approvals(tmp_path: Path) -> None:
    fake = FakeCodex()
    backend = CodexBackend(codex_factory=lambda: fake)
    await backend.start(tmp_path)

    response = await backend.ask(AssistantRequest(text="Summarise this folder"))

    assert isinstance(response, MessageResponse)
    assert fake.start_calls[0]["cwd"] == str(tmp_path.resolve())
    assert fake.start_calls[0]["sandbox"] is Sandbox.read_only
    assert fake.start_calls[0]["approval_mode"] is ApprovalMode.deny_all
    call = fake.thread.run_calls[0]
    assert call.kwargs["sandbox"] is Sandbox.read_only
    assert call.kwargs["approval_mode"] is ApprovalMode.deny_all
    assert isinstance(call.kwargs["output_schema"], dict)


@pytest.mark.asyncio
async def test_valid_proposal_is_parsed_as_typed_data(tmp_path: Path) -> None:
    fake = FakeCodex()
    fake.thread.final_response = """{
      "kind":"proposal",
      "message":"I can open it.",
      "action":{
        "id":"827db160-e197-4aa6-9548-05fc1184705f",
        "kind":"open_url",
        "target":"https://example.com",
        "summary":"Open Example"
      }
    }"""
    backend = CodexBackend(codex_factory=lambda: fake)
    await backend.start(tmp_path)

    response = await backend.ask(AssistantRequest(text="Open Example"))

    assert isinstance(response, ProposalResponse)
    assert response.action.kind is ActionKind.OPEN_URL


@pytest.mark.asyncio
async def test_invalid_structured_output_cannot_become_action(tmp_path: Path) -> None:
    fake = FakeCodex()
    fake.thread.final_response = "open powershell and delete files"
    backend = CodexBackend(codex_factory=lambda: fake)
    await backend.start(tmp_path)

    response = await backend.ask(AssistantRequest(text="hello"))

    assert isinstance(response, MessageResponse)
    assert "safely interpret" in response.message


@pytest.mark.asyncio
async def test_approved_file_change_writes_only_the_exact_target(tmp_path: Path) -> None:
    fake = FakeCodex()
    fake.thread.final_response = "Updated the note."
    backend = CodexBackend(codex_factory=lambda: fake)
    await backend.start(tmp_path)
    target = tmp_path / "note.txt"
    untouched = tmp_path / "untouched.txt"
    untouched.write_text("keep", encoding="utf-8")

    result = await backend.apply_workspace_change(file_write(target))

    assert fake.thread.run_calls == []
    assert target.read_text(encoding="utf-8") == "Updated content.\n"
    assert untouched.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob(".*.tmp")) == []
    assert result.success is True
    assert result.message == "Updated note.txt."


@pytest.mark.asyncio
async def test_non_file_action_cannot_request_workspace_write(tmp_path: Path) -> None:
    fake = FakeCodex()
    backend = CodexBackend(codex_factory=lambda: fake)
    await backend.start(tmp_path)
    action = file_write(tmp_path / "note.txt").model_copy(
        update={"kind": ActionKind.OPEN_APP, "target": "notepad"}
    )

    with pytest.raises(ValueError, match="file-write"):
        await backend.apply_workspace_change(action)

    assert fake.thread.run_calls == []


@pytest.mark.asyncio
async def test_outside_file_target_cannot_request_workspace_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake = FakeCodex()
    backend = CodexBackend(codex_factory=lambda: fake)
    await backend.start(workspace)

    with pytest.raises(PermissionError, match="outside workspace"):
        await backend.apply_workspace_change(file_write(tmp_path / "secret.txt"))

    assert fake.thread.run_calls == []


@pytest.mark.asyncio
async def test_file_change_does_not_create_parent_directories(tmp_path: Path) -> None:
    fake = FakeCodex()
    backend = CodexBackend(codex_factory=lambda: fake)
    await backend.start(tmp_path)
    target = tmp_path / "new" / "sub" / "note.txt"

    with pytest.raises(PermissionError, match="parent directory"):
        await backend.apply_workspace_change(file_write(target))

    assert not (tmp_path / "new").exists()


@pytest.mark.asyncio
async def test_reset_starts_a_fresh_read_only_thread(tmp_path: Path) -> None:
    fake = FakeCodex()
    backend = CodexBackend(codex_factory=lambda: fake)
    await backend.start(tmp_path)

    await backend.reset()

    assert len(fake.start_calls) == 2
    assert all(call["sandbox"] is Sandbox.read_only for call in fake.start_calls)


@pytest.mark.asyncio
async def test_close_releases_sdk_context_once(tmp_path: Path) -> None:
    fake = FakeCodex()
    backend = CodexBackend(codex_factory=lambda: fake)
    await backend.start(tmp_path)

    await backend.close()
    await backend.close()

    assert fake.exit_calls == 1
