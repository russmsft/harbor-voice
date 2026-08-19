"""Current-user Windows launch-at-login registration."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "HarborVoice"
_REG_SZ = 1


@dataclass(frozen=True, slots=True)
class StartupEntry:
    value: str
    registry_type: int


def _read_run_entry() -> StartupEntry | None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            access=winreg.KEY_QUERY_VALUE,
        ) as key:
            value, registry_type = winreg.QueryValueEx(key, _VALUE_NAME)
    except FileNotFoundError:
        return None
    return StartupEntry(str(value), registry_type)


def _write_run_entry(entry: StartupEntry) -> None:
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        _RUN_KEY,
        access=winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, _VALUE_NAME, 0, entry.registry_type, entry.value)


def _delete_run_value() -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            access=winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
    except FileNotFoundError:
        return


def current_launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return subprocess.list2cmdline([sys.executable, "-m", "harbor_voice.app"])


class StartupRegistration:
    def __init__(
        self,
        *,
        command: str | None = None,
        read_entry: Callable[[], StartupEntry | None] = _read_run_entry,
        write_entry: Callable[[StartupEntry], None] = _write_run_entry,
        delete_value: Callable[[], None] = _delete_run_value,
    ) -> None:
        self.command = command or current_launch_command()
        self._read_entry = read_entry
        self._write_entry = write_entry
        self._delete_value = delete_value

    def is_enabled(self) -> bool:
        configured = self.configured_entry()
        return (
            configured is not None
            and configured.value.casefold() == self.command.casefold()
        )

    def configured_entry(self) -> StartupEntry | None:
        return self._read_entry()

    def set_enabled(self, enabled: bool) -> bool:
        configured = self.configured_entry()
        if enabled:
            if (
                configured is not None
                and configured.value.casefold() != self.command.casefold()
            ):
                raise RuntimeError(
                    "the HarborVoice launch entry is owned by a different command"
                )
            if configured is not None:
                return False
            self._write_entry(StartupEntry(self.command, _REG_SZ))
            return True
        if (
            configured is not None
            and configured.value.casefold() == self.command.casefold()
        ):
            self._delete_value()
            return True
        return False

    def restore(self, entry: StartupEntry | None) -> None:
        configured = self.configured_entry()
        if entry is None:
            if (
                configured is not None
                and configured.value.casefold() == self.command.casefold()
            ):
                self._delete_value()
            return
        if configured == entry:
            return
        if (
            configured is not None
            and configured.value.casefold() != self.command.casefold()
        ):
            raise RuntimeError("the HarborVoice launch entry changed during rollback")
        self._write_entry(entry)
