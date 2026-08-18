from __future__ import annotations

from dataclasses import dataclass, field

from harbor_voice.domain import (
    ActionProposal,
    ActionResult,
    AssistantRequest,
    AssistantResponse,
    AudioRecording,
    MessageResponse,
    Transcript,
)


@dataclass
class FakeTranscriber:
    events: list[str]
    result: Transcript = field(default_factory=lambda: Transcript("hello", confidence=0.9))

    async def transcribe(self, recording: AudioRecording) -> Transcript:
        self.events.append("transcriber.transcribe")
        return self.result


@dataclass
class FakeBackend:
    events: list[str]
    response: AssistantResponse = field(
        default_factory=lambda: MessageResponse(kind="message", message="hello")
    )
    requests: list[AssistantRequest] = field(default_factory=list)
    reset_calls: int = 0
    close_calls: int = 0

    async def ask(self, request: AssistantRequest) -> AssistantResponse:
        self.events.append("backend.ask")
        self.requests.append(request)
        return self.response

    async def reset(self) -> None:
        self.events.append("backend.reset")
        self.reset_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


@dataclass
class FakeSpeaker:
    events: list[str]
    spoken: list[str] = field(default_factory=list)
    cancel_calls: int = 0
    close_calls: int = 0

    async def speak(self, text: str) -> None:
        self.events.append("speaker.speak")
        self.spoken.append(text)

    def cancel(self) -> None:
        self.events.append("speaker.cancel")
        self.cancel_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


@dataclass
class FakeRunner:
    events: list[str]
    result: ActionResult = field(
        default_factory=lambda: ActionResult(success=True, message="Action complete.")
    )
    executed: list[ActionProposal] = field(default_factory=list)
    error: Exception | None = None

    async def execute(self, action: ActionProposal) -> ActionResult:
        self.events.append("runner.execute")
        self.executed.append(action)
        if self.error:
            raise self.error
        return self.result


def sample_recording() -> AudioRecording:
    return AudioRecording(pcm=b"\x00\x00" * 4_000, sample_rate=16_000)

