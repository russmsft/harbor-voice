from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from harbor_voice.diagnostics import CheckResult, SystemChecks, run_diagnostics
from harbor_voice.storage import SettingsStore


@dataclass
class FakeChecks:
    names: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)

    def _check(self, name: str) -> CheckResult:
        self.names.append(name)
        return CheckResult(name=name, status="ok", detail=f"{name} available")

    def runtime(self) -> CheckResult:
        return self._check("runtime")

    def codex(self) -> CheckResult:
        return self._check("codex")

    def workspace(self) -> CheckResult:
        return self._check("workspace")

    def microphone(self) -> CheckResult:
        return self._check("microphone")

    def voice(self) -> CheckResult:
        return self._check("voice")

    def hotkey(self) -> CheckResult:
        return self._check("hotkey")


def test_doctor_runs_expected_read_only_checks() -> None:
    checks = FakeChecks()

    report = run_diagnostics(checks)

    assert checks.names == ["runtime", "codex", "workspace", "microphone", "voice", "hotkey"]
    assert checks.effects == []
    assert report.ok is True


def test_diagnostic_report_is_valid_json() -> None:
    report = run_diagnostics(FakeChecks())

    payload = json.loads(report.to_json())

    assert payload[0] == {"name": "runtime", "status": "ok", "detail": "runtime available"}


def test_workspace_diagnostic_does_not_quarantine_invalid_settings(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")

    result = SystemChecks(SettingsStore(path)).workspace()

    assert result.status == "unavailable"
    assert path.read_text(encoding="utf-8") == "{not json"
    assert list(tmp_path.glob("settings.invalid-*.json")) == []
