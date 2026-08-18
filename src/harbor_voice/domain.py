"""Application-owned domain values with no UI or provider dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class AppState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    APPROVAL = "approval"
    SPEAKING = "speaking"
    MUTED = "muted"
    ERROR = "error"


class ActionKind(StrEnum):
    FILE_WRITE = "file_write"
    OPEN_APP = "open_app"
    OPEN_URL = "open_url"
    CLIPBOARD_READ = "clipboard_read"
    CLIPBOARD_REPLACE = "clipboard_replace"


@dataclass(frozen=True, slots=True)
class AudioRecording:
    pcm: bytes
    sample_rate: int
    channels: int = 1

    def __post_init__(self) -> None:
        if self.channels != 1:
            raise ValueError("recording must be mono")
        if self.sample_rate <= 0:
            raise ValueError("sample rate must be positive")
        if len(self.pcm) % 2:
            raise ValueError("recording must contain complete 16-bit PCM samples")


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class AssistantRequest:
    text: str


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionProposal(_StrictModel):
    id: UUID
    kind: ActionKind
    target: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    payload: dict[str, str] = Field(default_factory=dict)


class MessageResponse(_StrictModel):
    kind: Literal["message"]
    message: str = Field(min_length=1)


class ProposalResponse(_StrictModel):
    kind: Literal["proposal"]
    message: str = ""
    action: ActionProposal


AssistantResponse = Annotated[
    MessageResponse | ProposalResponse,
    Field(discriminator="kind"),
]
_RESPONSE_ADAPTER = TypeAdapter(AssistantResponse)


@dataclass(frozen=True, slots=True)
class ActionResult:
    success: bool
    message: str
    details: dict[str, str] = field(default_factory=dict)


def parse_assistant_response(raw: str) -> AssistantResponse:
    """Parse a model response; malformed or expanded envelopes are rejected."""
    return _RESPONSE_ADAPTER.validate_json(raw)

