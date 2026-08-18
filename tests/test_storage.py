from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from harbor_voice.storage import (
    AppPaths,
    AssistantSettings,
    HistoryEntry,
    HistoryStore,
    PrivacyFilter,
    Retention,
    SettingsStore,
    configure_logging,
)


def test_missing_settings_use_private_defaults(tmp_path: Path) -> None:
    settings = SettingsStore(tmp_path / "settings.json").load()

    assert settings.workspace is None
    assert settings.retention is Retention.SESSION
    assert settings.launch_at_login is False
    assert settings.ptt_key == "f9"


def test_settings_round_trip_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "settings.json"
    store = SettingsStore(path)
    expected = AssistantSettings(workspace=tmp_path, ptt_key="f10")

    store.save(expected)

    assert store.load() == expected
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["ptt_key"] == "f10"


def test_invalid_settings_are_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    now = datetime(2026, 8, 18, 12, 30, tzinfo=UTC)

    settings = SettingsStore(path, clock=lambda: now).load()

    assert settings == AssistantSettings()
    assert not path.exists()
    assert (tmp_path / "settings.invalid-20260818-123000.json").read_text(
        encoding="utf-8"
    ) == "{not json"


def test_configured_workspace_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="workspace must exist"):
        AssistantSettings(workspace=tmp_path / "missing")


def test_session_history_never_touches_disk(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    store = HistoryStore(path, Retention.SESSION)

    store.append(HistoryEntry(role="user", text="hello"))

    assert not path.exists()
    assert store.read() == []


def test_seven_day_history_prunes_expired_entries(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    clock = [datetime(2026, 8, 1, tzinfo=UTC)]
    store = HistoryStore(path, Retention.SEVEN_DAYS, clock=lambda: clock[0])
    store.append(HistoryEntry(role="user", text="old"))
    clock[0] += timedelta(days=8)

    store.append(HistoryEntry(role="assistant", text="new"))

    entries = store.read()
    assert [entry.text for entry in entries] == ["new"]


def test_indefinite_history_retains_entries(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    clock = [datetime(2020, 1, 1, tzinfo=UTC)]
    store = HistoryStore(path, Retention.INDEFINITE, clock=lambda: clock[0])
    store.append(HistoryEntry(role="user", text="old"))
    clock[0] = datetime(2026, 8, 18, tzinfo=UTC)

    assert [entry.text for entry in store.read()] == ["old"]


@pytest.mark.parametrize("field", ["audio", "prompt", "response", "clipboard", "token", "secret"])
def test_privacy_filter_redacts_sensitive_record_fields(field: str) -> None:
    record = logging.LogRecord("harbor", logging.INFO, __file__, 1, "turn", (), None)
    setattr(record, field, "DO-NOT-LOG")

    assert PrivacyFilter().filter(record) is True
    assert getattr(record, field) == "[redacted]"


def test_app_paths_keep_data_below_selected_root(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path)

    assert paths.settings == tmp_path / "settings.json"
    assert paths.history == tmp_path / "history.jsonl"
    assert paths.log_dir == tmp_path / "logs"
    assert paths.models == tmp_path / "models"


def test_configured_log_omits_sensitive_extra_fields(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path)
    logger = configure_logging(paths)

    logger.info("turn complete", extra={"prompt": "DO-NOT-LOG"})
    for handler in logger.handlers:
        handler.flush()

    content = (paths.log_dir / "harbor-voice.log").read_text(encoding="utf-8")
    assert "turn complete" in content
    assert "DO-NOT-LOG" not in content
