"""Compact conversation, approval, and settings windows."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from PySide6.QtCore import Signal
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

from harbor_voice.domain import ActionProposal, AppState
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

    def closeEvent(self, event) -> None:
        self.hide()
        event.ignore()


class ApprovalDialog(QDialog):
    approved = Signal(object)
    rejected = Signal(object)

    def __init__(self, action: ActionProposal, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.action_id: UUID = action.id
        self.setWindowTitle("Approve Harbor Voice action")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Harbor Voice is requesting permission:"))
        self.summary_label = QLabel(action.summary)
        self.summary_label.setWordWrap(True)
        self.target_label = QLabel(action.target)
        self.target_label.setTextInteractionFlags(self.target_label.textInteractionFlags())
        self.target_label.setWordWrap(True)
        self.status_label = QLabel("Approval expires after five minutes.")
        self.approve_button = QPushButton("Approve once")
        self.reject_button = QPushButton("Reject")
        buttons = QDialogButtonBox()
        buttons.addButton(self.approve_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.reject_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.target_label)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)
        self.approve_button.clicked.connect(self._approve)
        self.reject_button.clicked.connect(self._reject)

    def expire(self) -> None:
        self.approve_button.setEnabled(False)
        self.status_label.setText("This approval has expired.")

    def _approve(self) -> None:
        self.approved.emit(self.action_id)
        self.accept()

    def _reject(self) -> None:
        self.rejected.emit(self.action_id)
        self.reject()


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
