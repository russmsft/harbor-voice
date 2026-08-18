"""Validated local settings, private history, and operational logging."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class Retention(StrEnum):
    SESSION = "session"
    SEVEN_DAYS = "seven_days"
    THIRTY_DAYS = "thirty_days"
    INDEFINITE = "indefinite"


class AssistantSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: Path | None = None
    ptt_key: str = Field(default="f9", min_length=1)
    microphone_device: int | str | None = None
    voice_id: str | None = None
    stt_model: str = Field(default="base.en", min_length=1)
    stt_device: str = Field(default="auto", min_length=1)
    stt_compute_type: str = Field(default="int8", min_length=1)
    retention: Retention = Retention.SESSION
    launch_at_login: bool = False
    registered_apps: dict[str, Path] = Field(default_factory=dict)

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        resolved = value.expanduser().resolve(strict=False)
        if not resolved.is_dir():
            raise ValueError("workspace must exist and be a directory")
        return resolved


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    settings: Path
    history: Path
    log_dir: Path
    models: Path

    @classmethod
    def from_root(cls, root: Path) -> AppPaths:
        resolved = root.expanduser().resolve(strict=False)
        return cls(
            root=resolved,
            settings=resolved / "settings.json",
            history=resolved / "history.jsonl",
            log_dir=resolved / "logs",
            models=resolved / "models",
        )


class SettingsStore:
    def __init__(self, path: Path, *, clock=lambda: datetime.now(UTC)) -> None:
        self.path = path
        self._clock = clock

    def load(self) -> AssistantSettings:
        if not self.path.exists():
            return AssistantSettings()
        try:
            return AssistantSettings.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            timestamp = self._clock().astimezone(UTC).strftime("%Y%m%d-%H%M%S")
            quarantine = self.path.with_name(f"settings.invalid-{timestamp}.json")
            os.replace(self.path, quarantine)
            return AssistantSettings()

    def save(self, settings: AssistantSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(settings.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1)
    timestamp: datetime | None = None


class HistoryStore:
    def __init__(
        self,
        path: Path,
        retention: Retention,
        *,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self.path = path
        self.retention = retention
        self._clock = clock

    def append(self, entry: HistoryEntry) -> None:
        if self.retention is Retention.SESSION:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamped = entry.model_copy(update={"timestamp": self._clock()})
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(stamped.model_dump_json() + "\n")
        self._prune()

    def read(self) -> list[HistoryEntry]:
        if self.retention is Retention.SESSION or not self.path.exists():
            return []
        return self._prune()

    def _prune(self) -> list[HistoryEntry]:
        entries = self._load_lines()
        cutoff = self._cutoff()
        if cutoff is not None:
            entries = [
                entry
                for entry in entries
                if entry.timestamp is not None and entry.timestamp >= cutoff
            ]
        self._rewrite(entries)
        return entries

    def _load_lines(self) -> list[HistoryEntry]:
        if not self.path.exists():
            return []
        entries: list[HistoryEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(HistoryEntry.model_validate_json(line))
            except ValidationError:
                continue
        return entries

    def _cutoff(self) -> datetime | None:
        if self.retention is Retention.SEVEN_DAYS:
            return self._clock() - timedelta(days=7)
        if self.retention is Retention.THIRTY_DAYS:
            return self._clock() - timedelta(days=30)
        return None

    def _rewrite(self, entries: list[HistoryEntry]) -> None:
        if not self.path.exists() and not entries:
            return
        content = "".join(entry.model_dump_json() + "\n" for entry in entries)
        self.path.write_text(content, encoding="utf-8", newline="\n")


class PrivacyFilter(logging.Filter):
    _SENSITIVE_FIELDS = frozenset(
        {"audio", "prompt", "response", "clipboard", "token", "secret"}
    )

    def filter(self, record: logging.LogRecord) -> bool:
        for field in self._SENSITIVE_FIELDS:
            if hasattr(record, field):
                setattr(record, field, "[redacted]")
        return True


def configure_logging(paths: AppPaths) -> logging.Logger:
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"harbor_voice.{hash(paths.root)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing in logger.handlers[:]:
        existing.close()
        logger.removeHandler(existing)
    handler = RotatingFileHandler(
        paths.log_dir / "harbor-voice.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.addFilter(PrivacyFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger
