from pathlib import Path
from uuid import UUID

import pytest

from harbor_voice.domain import ActionKind, ActionProposal
from harbor_voice.policy import Disposition, PermissionPolicy


def proposal(
    kind: ActionKind,
    target: str,
    *,
    payload: dict[str, str] | None = None,
) -> ActionProposal:
    return ActionProposal(
        id=UUID("ce580958-f241-4133-bf5e-8dcfb70b3729"),
        kind=kind,
        target=target,
        summary=f"Perform {kind.value}",
        payload=payload or {},
    )


@pytest.mark.parametrize("relative", ["notes.txt", "folder/report.md"])
def test_workspace_file_write_requires_confirmation(tmp_path: Path, relative: str) -> None:
    policy = PermissionPolicy(tmp_path, registered_apps={})

    decision = policy.evaluate(proposal(ActionKind.FILE_WRITE, str(tmp_path / relative)), now=100)

    assert decision.disposition is Disposition.CONFIRM
    assert decision.normalized_target == str((tmp_path / relative).resolve())


def test_prefix_escape_is_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    policy = PermissionPolicy(workspace, registered_apps={})

    decision = policy.evaluate(
        proposal(ActionKind.FILE_WRITE, str(tmp_path / "work-secret" / "x.txt")),
        now=100,
    )

    assert decision.disposition is Disposition.BLOCK
    assert decision.reason == "outside_workspace"


def test_relative_escape_is_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    policy = PermissionPolicy(workspace, registered_apps={})

    decision = policy.evaluate(proposal(ActionKind.FILE_WRITE, "../secret.txt"), now=100)

    assert decision.disposition is Disposition.BLOCK


def test_nonexistent_workspace_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workspace must exist"):
        PermissionPolicy(tmp_path / "missing", registered_apps={})


@pytest.mark.parametrize(
    ("url", "expected", "reason"),
    [
        ("https://example.com/a", Disposition.CONFIRM, "confirmation_required"),
        ("http://example.com", Disposition.BLOCK, "scheme_blocked"),
        ("file:///C:/Windows/win.ini", Disposition.BLOCK, "scheme_blocked"),
        ("https://user:pass@example.com", Disposition.BLOCK, "url_credentials_blocked"),
        ("https:///missing-host", Disposition.BLOCK, "missing_url_host"),
    ],
)
def test_only_plain_https_urls_can_be_confirmed(
    tmp_path: Path,
    url: str,
    expected: Disposition,
    reason: str,
) -> None:
    decision = PermissionPolicy(tmp_path, {}).evaluate(
        proposal(ActionKind.OPEN_URL, url),
        now=100,
    )

    assert decision.disposition is expected
    assert decision.reason == reason


def test_registered_application_requires_confirmation(tmp_path: Path) -> None:
    executable = tmp_path / "notepad.exe"
    executable.write_bytes(b"MZ")
    policy = PermissionPolicy(tmp_path, {"Notepad": executable})

    decision = policy.evaluate(proposal(ActionKind.OPEN_APP, "notepad"), now=100)

    assert decision.disposition is Disposition.CONFIRM
    assert decision.normalized_target == str(executable.resolve())


def test_unknown_application_is_blocked(tmp_path: Path) -> None:
    decision = PermissionPolicy(tmp_path, {}).evaluate(
        proposal(ActionKind.OPEN_APP, "PowerShell"),
        now=100,
    )

    assert decision.disposition is Disposition.BLOCK
    assert decision.reason == "app_not_registered"


def test_clipboard_read_is_automatic_but_replace_requires_confirmation(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path, {})

    read = policy.evaluate(proposal(ActionKind.CLIPBOARD_READ, "clipboard"), now=100)
    replace = policy.evaluate(
        proposal(ActionKind.CLIPBOARD_REPLACE, "clipboard", payload={"text": "hello"}),
        now=100,
    )

    assert read.disposition is Disposition.AUTO
    assert replace.disposition is Disposition.CONFIRM


def test_large_clipboard_replacement_is_blocked(tmp_path: Path) -> None:
    action = proposal(
        ActionKind.CLIPBOARD_REPLACE,
        "clipboard",
        payload={"text": "x" * 100_001},
    )

    decision = PermissionPolicy(tmp_path, {}).evaluate(action, now=100)

    assert decision.disposition is Disposition.BLOCK
    assert decision.reason == "clipboard_too_large"


def test_approval_expires_after_five_minutes(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path, {})
    action = proposal(ActionKind.OPEN_URL, "https://example.com")

    fresh = policy.revalidate(action, approved_at=100, now=400)
    expired = policy.revalidate(action, approved_at=100, now=400.001)

    assert fresh.disposition is Disposition.CONFIRM
    assert expired.disposition is Disposition.BLOCK
    assert expired.reason == "approval_expired"


def test_backwards_clock_invalidates_approval(tmp_path: Path) -> None:
    decision = PermissionPolicy(tmp_path, {}).revalidate(
        proposal(ActionKind.OPEN_URL, "https://example.com"),
        approved_at=100,
        now=99,
    )

    assert decision.disposition is Disposition.BLOCK
    assert decision.reason == "clock_changed"

