from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from harbor_voice.domain import ActionKind, ActionProposal, AppState
from harbor_voice.ui.tray import SingleInstanceGuard, TrayController
from harbor_voice.ui.window import ApprovalDialog, ConversationWindow, SettingsDialog


def proposal() -> ActionProposal:
    return ActionProposal(
        id=UUID("f05128c4-8919-4b89-9a46-9d80f409bf25"),
        kind=ActionKind.OPEN_URL,
        target="https://example.com/private",
        summary="Open the private page",
    )


@pytest.mark.parametrize("state", list(AppState))
def test_tray_state_updates_visible_tooltip(state: AppState, qapp) -> None:
    del qapp
    tray = TrayController()

    tray.set_state(state)

    assert tray.icon.toolTip() == f"Harbor Voice — {state.value}"


def test_tray_menu_emits_show_request(qtbot: QtBot) -> None:
    tray = TrayController()
    with qtbot.waitSignal(tray.show_requested):
        tray.show_action.trigger()


def test_approval_dialog_shows_exact_target(qtbot: QtBot) -> None:
    dialog = ApprovalDialog(proposal())
    qtbot.addWidget(dialog)

    assert dialog.target_label.text() == "https://example.com/private"
    assert dialog.summary_label.text() == "Open the private page"


def test_approval_dialog_emits_matching_identifier(qtbot: QtBot) -> None:
    dialog = ApprovalDialog(proposal())
    qtbot.addWidget(dialog)
    received: list[UUID] = []
    dialog.approved.connect(received.append)

    qtbot.mouseClick(dialog.approve_button, Qt.MouseButton.LeftButton)

    assert received == [proposal().id]


def test_expired_approval_disables_execution(qtbot: QtBot) -> None:
    dialog = ApprovalDialog(proposal())
    qtbot.addWidget(dialog)

    dialog.expire()

    assert not dialog.approve_button.isEnabled()
    assert "expired" in dialog.status_label.text().casefold()


def test_close_hides_conversation_without_quitting(qtbot: QtBot) -> None:
    window = ConversationWindow()
    qtbot.addWidget(window)
    window.show()

    window.close()

    assert window.isHidden()


def test_conversation_window_renders_turn_and_state(qtbot: QtBot) -> None:
    window = ConversationWindow()
    qtbot.addWidget(window)

    window.set_state(AppState.THINKING)
    window.set_turn("What is this?", "I am checking.")

    assert window.state_label.text() == "Thinking"
    assert window.transcript_text.toPlainText() == "What is this?"
    assert window.response_text.toPlainText() == "I am checking."


def test_settings_dialog_rejects_missing_workspace(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    dialog.workspace_edit.setText(str(tmp_path / "missing"))

    qtbot.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)

    assert "workspace must exist" in dialog.error_label.text().casefold()


def test_single_instance_lock_allows_only_one_owner(tmp_path: Path) -> None:
    first = SingleInstanceGuard(tmp_path / "harbor.lock")
    second = SingleInstanceGuard(tmp_path / "harbor.lock")

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()
