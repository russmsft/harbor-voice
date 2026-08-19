"""Read-only environment diagnostics for Harbor Voice."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from platformdirs import user_data_path

from harbor_voice.storage import AppPaths, SettingsStore


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    results: list[CheckResult]

    @property
    def ok(self) -> bool:
        return all(result.status == "ok" for result in self.results)

    def to_json(self) -> str:
        return json.dumps([asdict(result) for result in self.results], indent=2)


class DiagnosticChecks(Protocol):
    def runtime(self) -> CheckResult: ...

    def copilot(self) -> CheckResult: ...

    def workspace(self) -> CheckResult: ...

    def microphone(self) -> CheckResult: ...

    def voice(self) -> CheckResult: ...

    def hotkey(self) -> CheckResult: ...


def run_diagnostics(checks: DiagnosticChecks) -> DiagnosticReport:
    return DiagnosticReport(
        [
            checks.runtime(),
            checks.copilot(),
            checks.workspace(),
            checks.microphone(),
            checks.voice(),
            checks.hotkey(),
        ]
    )


class SystemChecks:
    def __init__(self, settings_store: SettingsStore) -> None:
        self._settings_store = settings_store

    def runtime(self) -> CheckResult:
        supported = (3, 11) <= sys.version_info[:2] < (3, 13)
        return CheckResult(
            "runtime",
            "ok" if supported else "unavailable",
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )

    def copilot(self) -> CheckResult:
        executable = shutil.which("copilot")
        if executable is None:
            return CheckResult("copilot", "unavailable", "GitHub Copilot CLI was not found")
        try:
            completed = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return CheckResult("copilot", "unavailable", str(exc))
        detail = (completed.stdout or completed.stderr).strip()
        status = "ok" if completed.returncode == 0 else "unavailable"
        return CheckResult("copilot", status, detail or "GitHub Copilot CLI returned no version")

    def workspace(self) -> CheckResult:
        workspace = self._settings_store.load(quarantine_invalid=False).workspace
        if workspace is None:
            return CheckResult("workspace", "unavailable", "No working folder selected")
        return CheckResult("workspace", "ok", str(workspace))

    def microphone(self) -> CheckResult:
        try:
            import sounddevice as sd

            device = sd.query_devices(kind="input")
        except Exception as exc:
            return CheckResult("microphone", "unavailable", str(exc))
        return CheckResult("microphone", "ok", str(device.get("name", "default input")))

    def voice(self) -> CheckResult:
        try:
            import pyttsx3

            engine = pyttsx3.init()
            voices = engine.getProperty("voices")
            engine.stop()
        except Exception as exc:
            return CheckResult("voice", "unavailable", str(exc))
        return CheckResult("voice", "ok", f"{len(voices)} SAPI voices available")

    def hotkey(self) -> CheckResult:
        try:
            import pynput  # noqa: F401
        except Exception as exc:
            return CheckResult("hotkey", "unavailable", str(exc))
        return CheckResult("hotkey", "ok", "Global hotkey library is installed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Harbor Voice without taking actions")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    paths = AppPaths.from_root(Path(user_data_path("HarborVoice")))
    report = run_diagnostics(SystemChecks(SettingsStore(paths.settings)))
    if args.as_json:
        print(report.to_json())
    else:
        for result in report.results:
            print(f"{result.name}: {result.status} — {result.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
