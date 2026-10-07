"""System tray presentation and process-instance lock."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from harbor_voice.domain import AppState

_STATE_COLORS = {
    AppState.IDLE: "#2dd4bf",
    AppState.LISTENING: "#38bdf8",
    AppState.TRANSCRIBING: "#818cf8",
    AppState.THINKING: "#c084fc",
    AppState.APPROVAL: "#fbbf24",
    AppState.SPEAKING: "#34d399",
    AppState.MUTED: "#94a3b8",
    AppState.ERROR: "#fb7185",
}


def _make_icon(state: AppState) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#0f172a"))
    painter.drawEllipse(2, 2, 60, 60)
    painter.setBrush(QColor(_STATE_COLORS[state]))
    painter.drawEllipse(14, 14, 36, 36)
    painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    show_requested = Signal()
    mute_requested = Signal()
    settings_requested = Signal()
    new_conversation_requested = Signal()
    meeting_requested = Signal()
    join_requested = Signal()
    answer_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.icon = QSystemTrayIcon(_make_icon(AppState.IDLE), self)
        self.menu = QMenu()
        self.show_action = self.menu.addAction("Show conversation")
        self.mute_action = self.menu.addAction("Mute microphone")
        self.settings_action = self.menu.addAction("Settings")
        self.new_action = self.menu.addAction("New conversation")
        self.menu.addSeparator()
        self.meeting_action = self.menu.addAction("Start meeting mode")
        self.join_action = self.menu.addAction("Send Scout to a meeting…")
        self.answer_action = self.menu.addAction("Answer latest meeting question")
        self.answer_action.setEnabled(False)
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("Quit")
        self.icon.setContextMenu(self.menu)
        self.show_action.triggered.connect(self.show_requested.emit)
        self.mute_action.triggered.connect(self.mute_requested.emit)
        self.settings_action.triggered.connect(self.settings_requested.emit)
        self.new_action.triggered.connect(self.new_conversation_requested.emit)
        self.meeting_action.triggered.connect(self.meeting_requested.emit)
        self.join_action.triggered.connect(self.join_requested.emit)
        self.answer_action.triggered.connect(self.answer_requested.emit)
        self.quit_action.triggered.connect(self.quit_requested.emit)
        self.icon.activated.connect(self._activated)
        self.set_state(AppState.IDLE)

    def set_meeting_active(self, active: bool, *, remote: bool = False) -> None:
        if active and remote:
            self.meeting_action.setText("Remove Scout from the meeting")
        else:
            self.meeting_action.setText("Stop meeting mode" if active else "Start meeting mode")
        self.join_action.setEnabled(not active)
        self.answer_action.setEnabled(active)

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def set_state(self, state: AppState) -> None:
        self.icon.setIcon(_make_icon(state))
        self.icon.setToolTip(f"Harbor Voice — {state.value}")
        self.mute_action.setText(
            "Unmute microphone" if state is AppState.MUTED else "Mute microphone"
        )

    def notify(self, title: str, message: str) -> None:
        self.icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 4_000)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason is QSystemTrayIcon.ActivationReason.Trigger:
            self.show_requested.emit()


class SingleInstanceGuard:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = QLockFile(str(path))
        self._lock.setStaleLockTime(0)
        self._owned = False

    def acquire(self) -> bool:
        if self._owned:
            return True
        self._owned = self._lock.tryLock(0)
        return self._owned

    def release(self) -> None:
        if self._owned:
            self._lock.unlock()
            self._owned = False
