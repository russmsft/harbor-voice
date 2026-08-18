"""Least-privilege decisions for model-proposed actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from harbor_voice.domain import ActionKind, ActionProposal


class Disposition(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    disposition: Disposition
    reason: str
    normalized_target: str | None = None


class PermissionPolicy:
    """Evaluate typed actions without executing them."""

    def __init__(
        self,
        workspace: Path,
        registered_apps: dict[str, Path],
        *,
        approval_ttl_seconds: float = 300.0,
        max_clipboard_chars: int = 100_000,
    ) -> None:
        resolved_workspace = workspace.expanduser().resolve(strict=False)
        if not resolved_workspace.is_dir():
            raise ValueError("workspace must exist and be a directory")
        self.workspace = resolved_workspace
        self.registered_apps = {
            name.casefold(): path.expanduser().resolve(strict=False)
            for name, path in registered_apps.items()
        }
        self.approval_ttl_seconds = approval_ttl_seconds
        self.max_clipboard_chars = max_clipboard_chars

    def evaluate(self, action: ActionProposal, now: float) -> PolicyDecision:
        """Return the permission decision for an action at the current instant."""
        del now
        match action.kind:
            case ActionKind.FILE_WRITE:
                return self._file_write(action.target)
            case ActionKind.OPEN_APP:
                return self._open_app(action.target)
            case ActionKind.OPEN_URL:
                return self._open_url(action.target)
            case ActionKind.CLIPBOARD_READ:
                return PolicyDecision(Disposition.AUTO, "read_only", "clipboard")
            case ActionKind.CLIPBOARD_REPLACE:
                return self._clipboard_replace(action)
        return PolicyDecision(Disposition.BLOCK, "unsupported_action")

    def revalidate(
        self,
        action: ActionProposal,
        *,
        approved_at: float,
        now: float,
    ) -> PolicyDecision:
        """Re-evaluate target policy and the one-shot approval lifetime."""
        age = now - approved_at
        if age < 0:
            return PolicyDecision(Disposition.BLOCK, "clock_changed")
        if age > self.approval_ttl_seconds:
            return PolicyDecision(Disposition.BLOCK, "approval_expired")
        return self.evaluate(action, now)

    def _file_write(self, target_text: str) -> PolicyDecision:
        candidate = Path(target_text).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        target = candidate.resolve(strict=False)
        if target != self.workspace and not target.is_relative_to(self.workspace):
            return PolicyDecision(Disposition.BLOCK, "outside_workspace", str(target))
        return PolicyDecision(Disposition.CONFIRM, "confirmation_required", str(target))

    def _open_app(self, app_name: str) -> PolicyDecision:
        executable = self.registered_apps.get(app_name.casefold())
        if executable is None:
            return PolicyDecision(Disposition.BLOCK, "app_not_registered")
        if not executable.is_file():
            return PolicyDecision(Disposition.BLOCK, "app_not_found", str(executable))
        return PolicyDecision(
            Disposition.CONFIRM,
            "confirmation_required",
            str(executable),
        )

    @staticmethod
    def _open_url(target: str) -> PolicyDecision:
        parsed = urlsplit(target)
        if parsed.scheme.casefold() != "https":
            return PolicyDecision(Disposition.BLOCK, "scheme_blocked")
        if not parsed.hostname:
            return PolicyDecision(Disposition.BLOCK, "missing_url_host")
        if parsed.username or parsed.password:
            return PolicyDecision(Disposition.BLOCK, "url_credentials_blocked")
        normalized = urlunsplit(
            ("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
        )
        return PolicyDecision(Disposition.CONFIRM, "confirmation_required", normalized)

    def _clipboard_replace(self, action: ActionProposal) -> PolicyDecision:
        text = action.payload.get("text")
        if text is None:
            return PolicyDecision(Disposition.BLOCK, "clipboard_text_missing")
        if len(text) > self.max_clipboard_chars:
            return PolicyDecision(Disposition.BLOCK, "clipboard_too_large")
        return PolicyDecision(Disposition.CONFIRM, "confirmation_required", "clipboard")
