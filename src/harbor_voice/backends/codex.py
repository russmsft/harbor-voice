"""Persistent, structured Codex reasoning with explicit sandbox boundaries."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, Sandbox
from pydantic import TypeAdapter, ValidationError

from harbor_voice.domain import (
    ActionKind,
    ActionProposal,
    ActionResult,
    AssistantRequest,
    AssistantResponse,
    MessageResponse,
    parse_assistant_response,
)

VOICE_INSTRUCTIONS = """You are Harbor Voice's reasoning provider for one person.
Reply concisely in natural spoken language. Use the supplied JSON schema exactly.
Return a message for information and read-only work. Return a proposal when the user
explicitly asks to write a file, open an application, open an HTTPS URL, read the
clipboard, or replace clipboard text. Never claim an action has happened when you
are only proposing it. Never propose deletion, credentials, arbitrary shell access,
or work outside the current folder.
"""

ASSISTANT_RESPONSE_SCHEMA = TypeAdapter(AssistantResponse).json_schema()
_SAFE_PARSE_FAILURE = "I couldn't safely interpret that response, so I took no action."


def _default_codex_factory() -> AsyncCodex:
    return AsyncCodex()


class CodexBackend:
    def __init__(self, *, codex_factory: Callable[[], Any] = _default_codex_factory) -> None:
        self._codex_factory = codex_factory
        self._client = None
        self._thread = None
        self.workspace: Path | None = None

    async def start(self, workspace: Path) -> None:
        resolved = workspace.expanduser().resolve(strict=False)
        if not resolved.is_dir():
            raise ValueError("workspace must exist and be a directory")
        if self._client is not None:
            raise RuntimeError("Codex backend is already started")
        context = self._codex_factory()
        self._client = await context.__aenter__()
        self.workspace = resolved
        self._thread = await self._start_thread()

    async def ask(self, request: AssistantRequest) -> AssistantResponse:
        thread = self._require_thread()
        result = await thread.run(
            request.text,
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
            output_schema=ASSISTANT_RESPONSE_SCHEMA,
        )
        raw = result.final_response or ""
        try:
            return parse_assistant_response(raw)
        except ValidationError:
            return MessageResponse(kind="message", message=_SAFE_PARSE_FAILURE)

    async def apply_workspace_change(self, action: ActionProposal) -> ActionResult:
        if action.kind is not ActionKind.FILE_WRITE:
            raise ValueError("workspace-write is restricted to an approved file-write action")
        workspace = self._require_workspace()
        candidate = Path(action.target).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        target = candidate.resolve(strict=False)
        if target != workspace and not target.is_relative_to(workspace):
            raise PermissionError("file target is outside workspace")
        prompt = (
            "Execute only this user-approved file change. "
            f"Approval ID: {action.id}. Target: {target}. "
            f"Approved effect: {action.summary}. "
            "Do not edit any other path and do not perform unrelated work. "
            "Report the result in one short spoken sentence."
        )
        result = await self._require_thread().run(
            prompt,
            sandbox=Sandbox.workspace_write,
            approval_mode=ApprovalMode.deny_all,
        )
        message = (result.final_response or "The approved file change completed.").strip()
        return ActionResult(success=True, message=message, details={"target": str(target)})

    async def reset(self) -> None:
        self._require_workspace()
        self._thread = await self._start_thread()

    async def close(self) -> None:
        client, self._client = self._client, None
        self._thread = None
        if client is not None:
            await client.__aexit__(None, None, None)

    async def _start_thread(self):
        client = self._client
        workspace = self._require_workspace()
        if client is None:
            raise RuntimeError("Codex backend is not started")
        return await client.thread_start(
            cwd=str(workspace),
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
            developer_instructions=VOICE_INSTRUCTIONS,
        )

    def _require_thread(self):
        if self._thread is None:
            raise RuntimeError("Codex backend is not started")
        return self._thread

    def _require_workspace(self) -> Path:
        if self.workspace is None:
            raise RuntimeError("Codex backend is not started")
        return self.workspace

