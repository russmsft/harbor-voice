from __future__ import annotations

import json
from dataclasses import dataclass, field

from harbor_voice.diagnostics import CheckResult, run_diagnostics


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

