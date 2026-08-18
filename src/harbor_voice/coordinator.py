"""Interruptible application state machine for one voice conversation."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from harbor_voice.domain import (
    ActionProposal,
    AppState,
    AssistantRequest,
    AudioRecording,
    MessageResponse,
    ProposalResponse,
)
from harbor_voice.policy import Disposition, PermissionPolicy
from harbor_voice.ports import ActionRunner, AssistantBackend, Speaker, StateSink, Transcriber


class ApprovalNotFound(LookupError):
    """The supplied identifier does not match the pending action."""


class ApprovalExpired(PermissionError):
    """The pending action is no longer eligible for execution."""


@dataclass(frozen=True, slots=True)
class PendingApproval:
    action: ActionProposal
    created_at: float


class TurnCoordinator:
    def __init__(
        self,
        *,
        transcriber: Transcriber,
        backend: AssistantBackend,
        speaker: Speaker,
        runner: ActionRunner,
        policy: PermissionPolicy,
        state_sink: StateSink,
        clock=monotonic,
    ) -> None:
        self._transcriber = transcriber
        self._backend = backend
        self._speaker = speaker
        self._runner = runner
        self._policy = policy
        self._state_sink = state_sink
        self._clock = clock
        self._pending: PendingApproval | None = None
        self._state = AppState.IDLE
        self.last_error: str | None = None

    @property
    def pending(self) -> PendingApproval | None:
        return self._pending

    @property
    def state(self) -> AppState:
        return self._state

    async def submit(self, recording: AudioRecording) -> None:
        """Transcribe and process one in-memory recording."""
        self._speaker.cancel()
        self.last_error = None
        try:
            self._publish(AppState.TRANSCRIBING)
            transcript = await self._transcriber.transcribe(recording)
            text = transcript.text.strip()
            if not text:
                self._publish(AppState.IDLE)
                return

            self._publish(AppState.THINKING)
            response = await self._backend.ask(AssistantRequest(text=text))
            if isinstance(response, MessageResponse):
                await self._speak(response.message)
                return
            await self._handle_proposal(response)
        except Exception as exc:
            self._fail(exc)

    async def approve(self, action_id: UUID) -> None:
        """Execute the matching pending action at most once."""
        pending = self._pending
        if pending is None or pending.action.id != action_id:
            raise ApprovalNotFound(str(action_id))

        decision = self._policy.revalidate(
            pending.action,
            approved_at=pending.created_at,
            now=self._clock(),
        )
        if decision.disposition is not Disposition.CONFIRM:
            self._pending = None
            self._publish(AppState.IDLE)
            raise ApprovalExpired(decision.reason)

        self._pending = None
        try:
            await self._execute(pending.action)
        except Exception as exc:
            self._fail(exc)

    def reject(self, action_id: UUID) -> None:
        pending = self._pending
        if pending is None or pending.action.id != action_id:
            raise ApprovalNotFound(str(action_id))
        self._pending = None
        self._publish(AppState.IDLE)

    def cancel_speech(self) -> None:
        self._speaker.cancel()

    async def new_conversation(self) -> None:
        self._speaker.cancel()
        self._pending = None
        self.last_error = None
        await self._backend.reset()
        self._publish(AppState.IDLE)

    def recover(self) -> None:
        self.last_error = None
        self._publish(AppState.IDLE)

    async def _handle_proposal(self, response: ProposalResponse) -> None:
        decision = self._policy.evaluate(response.action, now=self._clock())
        if decision.disposition is Disposition.BLOCK:
            reason = decision.reason.replace("_", " ")
            await self._speak(f"I can't do that because it is blocked: {reason}.")
            return
        if decision.disposition is Disposition.AUTO:
            await self._execute(response.action)
            return
        self._pending = PendingApproval(response.action, self._clock())
        self._publish(AppState.APPROVAL)

    async def _execute(self, action: ActionProposal) -> None:
        self._publish(AppState.THINKING)
        result = await self._runner.execute(action)
        await self._speak(result.message)

    async def _speak(self, text: str) -> None:
        if not text.strip():
            self._publish(AppState.IDLE)
            return
        self._publish(AppState.SPEAKING)
        await self._speaker.speak(text)
        self._publish(AppState.IDLE)

    def _fail(self, error: Exception) -> None:
        self.last_error = str(error) or type(error).__name__
        self._publish(AppState.ERROR)

    def _publish(self, state: AppState) -> None:
        self._state = state
        self._state_sink(state)

