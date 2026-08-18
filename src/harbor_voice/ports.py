"""Dependency-inversion ports used by the turn coordinator."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from harbor_voice.domain import (
    ActionProposal,
    ActionResult,
    AppState,
    AssistantRequest,
    AssistantResponse,
    AudioRecording,
    Transcript,
)


class Transcriber(Protocol):
    async def transcribe(self, recording: AudioRecording) -> Transcript: ...


class AssistantBackend(Protocol):
    async def ask(self, request: AssistantRequest) -> AssistantResponse: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


class Speaker(Protocol):
    async def speak(self, text: str) -> None: ...

    def cancel(self) -> None: ...

    async def close(self) -> None: ...


class ActionRunner(Protocol):
    async def execute(self, action: ActionProposal) -> ActionResult: ...


StateSink = Callable[[AppState], None]
