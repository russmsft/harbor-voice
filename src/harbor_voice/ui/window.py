"""Compact conversation, approval, and settings windows."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from harbor_voice.domain import ActionKind, ActionProposal, AppState
from harbor_voice.storage import AssistantSettings, Retention


class ConversationWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Harbor Voice")
        self.resize(520, 430)
        body = QWidget(self)
        layout = QVBoxLayout(body)
        self.state_label = QLabel("Idle")
        self.state_label.setObjectName("stateLabel")
        self.transcript_text = QTextEdit()
        self.transcript_text.setReadOnly(True)
        self.transcript_text.setPlaceholderText("Your latest request")
        self.response_text = QTextEdit()
        self.response_text.setReadOnly(True)
        self.response_text.setPlaceholderText("Harbor Voice's latest response")
        layout.addWidget(self.state_label)
        layout.addWidget(QLabel("You"))
        layout.addWidget(self.transcript_text)
        layout.addWidget(QLabel("Harbor Voice"))
        layout.addWidget(self.response_text)
        self.setCentralWidget(body)
        self.setStyleSheet(
            "QWidget { background: #0f172a; color: #e2e8f0; }"
            "QTextEdit, QLineEdit, QComboBox { background: #1e293b; border: 1px solid #334155; "
            "border-radius: 6px; padding: 6px; }"
            "#stateLabel { color: #5eead4; font-size: 16px; font-weight: 600; }"
        )

    def set_state(self, state: AppState) -> None:
        self.state_label.setText(state.value.capitalize())

    def set_turn(self, transcript: str, response: str) -> None:
        self.transcript_text.setPlainText(transcript)
        self.response_text.setPlainText(response)

    def set_error(self, message: str) -> None:
        self.response_text.setPlainText(f"Error: {message}")

    def closeEvent(self, event) -> None:
        self.hide()
        event.ignore()


class ApprovalDialog(QDialog):
    action_approved = Signal(object)
    action_rejected = Signal(object)
    action_expired = Signal(object)

    def __init__(
        self,
        action: ActionProposal,
        parent: QWidget | None = None,
        *,
        display_target: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.action_id: UUID = action.id
        self._resolved = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Approve Harbor Voice action")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Harbor Voice is requesting permission:"))
        self.kind_label = QLabel(action.kind.value.replace("_", " ").title())
        self.summary_label = QLabel(action.summary)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        self.target_label = QLabel(display_target or action.target)
        self.target_label.setTextFormat(Qt.TextFormat.PlainText)
        self.target_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.target_label.setWordWrap(True)
        self.effect_label = QLabel(self._describe_effect(action))
        self.effect_label.setTextFormat(Qt.TextFormat.PlainText)
        self.effect_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.effect_label.setWordWrap(True)
        self.status_label = QLabel("Approval expires after five minutes.")
        self.approve_button = QPushButton("Approve once")
        self.reject_button = QPushButton("Reject")
        buttons = QDialogButtonBox()
        buttons.addButton(self.approve_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.reject_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(QLabel("Action"))
        layout.addWidget(self.kind_label)
        layout.addWidget(QLabel("Assistant summary"))
        layout.addWidget(self.summary_label)
        layout.addWidget(QLabel("Authoritative target"))
        layout.addWidget(self.target_label)
        layout.addWidget(QLabel("Effect"))
        layout.addWidget(self.effect_label)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)
        self.approve_button.clicked.connect(self._approve)
        self.reject_button.clicked.connect(self.reject)
        self.expiry_timer = QTimer(self)
        self.expiry_timer.setSingleShot(True)
        self.expiry_timer.setInterval(300_000)
        self.expiry_timer.timeout.connect(self.expire)
        self.expiry_timer.start()

    def expire(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        self.approve_button.setEnabled(False)
        self.status_label.setText("This approval has expired.")
        self.action_expired.emit(self.action_id)
        super().reject()

    def _approve(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        self.action_approved.emit(self.action_id)
        super().accept()

    def reject(self) -> None:
        if not self._resolved:
            self._resolved = True
            self.action_rejected.emit(self.action_id)
        super().reject()

    @staticmethod
    def _describe_effect(action: ActionProposal) -> str:
        if action.kind is ActionKind.FILE_WRITE:
            content = action.payload.get("content", "")
            preview = content[:500]
            suffix = "\n[preview truncated]" if len(content) > len(preview) else ""
            return (
                f"Create or replace one file with {len(content)} UTF-8 characters:\n"
                f"{preview}{suffix}"
            )
        if action.kind is ActionKind.CLIPBOARD_REPLACE:
            text = action.payload.get("text", "")
            preview = text[:500]
            suffix = "\n[preview truncated]" if len(text) > len(preview) else ""
            return f"Replace the clipboard with {len(text)} characters:\n{preview}{suffix}"
        if action.kind is ActionKind.OPEN_APP:
            return "Launch the registered executable shown above without command-line arguments."
        if action.kind is ActionKind.OPEN_URL:
            return "Open the HTTPS URL shown above in the default browser."
        return "Perform the typed action shown above."


class SettingsDialog(QDialog):
    saved = Signal(object)

    def __init__(
        self,
        settings: AssistantSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        current = settings or AssistantSettings()
        self._current = current
        self.setWindowTitle("Harbor Voice settings")
        layout = QFormLayout(self)
        self.workspace_edit = QLineEdit(str(current.workspace or ""))
        self.ptt_edit = QLineEdit(current.ptt_key)
        self.retention_combo = QComboBox()
        for retention in Retention:
            self.retention_combo.addItem(retention.value.replace("_", " ").title(), retention)
        self.retention_combo.setCurrentIndex(self.retention_combo.findData(current.retention))
        self.launch_checkbox = QCheckBox("Launch when I sign in")
        self.launch_checkbox.setChecked(current.launch_at_login)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #fb7185")
        self.save_button = QPushButton("Save")
        layout.addRow("Working folder", self.workspace_edit)
        layout.addRow("Push-to-talk key", self.ptt_edit)
        layout.addRow("Transcript retention", self.retention_combo)
        layout.addRow("", self.launch_checkbox)
        layout.addRow("", self.error_label)
        layout.addRow("", self.save_button)
        self.save_button.clicked.connect(self._save)

    def _save(self) -> None:
        workspace_text = self.workspace_edit.text().strip()
        try:
            values = self._current.model_dump()
            values.update(
                workspace=Path(workspace_text) if workspace_text else None,
                ptt_key=self.ptt_edit.text().strip(),
                retention=self.retention_combo.currentData(),
                launch_at_login=self.launch_checkbox.isChecked(),
            )
            settings = AssistantSettings.model_validate(values)
        except ValidationError as exc:
            self.error_label.setText(str(exc.errors()[0]["msg"]))
            return
        self.error_label.clear()
        self.saved.emit(settings)
        self.accept()
