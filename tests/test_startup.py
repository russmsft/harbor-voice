import pytest

import harbor_voice.startup
from harbor_voice.startup import StartupEntry, StartupRegistration


def test_enabling_startup_writes_exact_command() -> None:
    writes: list[StartupEntry] = []
    registration = StartupRegistration(
        command='"C:\\Program Files\\Harbor Voice\\HarborVoice.exe"',
        read_entry=lambda: None,
        write_entry=writes.append,
    )

    registration.set_enabled(True)

    assert writes == [
        StartupEntry('"C:\\Program Files\\Harbor Voice\\HarborVoice.exe"', 1)
    ]


def test_disabling_startup_removes_only_matching_registration() -> None:
    deletes: list[bool] = []
    command = '"C:\\Program Files\\Harbor Voice\\HarborVoice.exe"'
    matching = StartupRegistration(
        command=command,
        read_entry=lambda: StartupEntry(command.lower(), 1),
        delete_value=lambda: deletes.append(True),
    )
    unrelated = StartupRegistration(
        command=command,
        read_entry=lambda: StartupEntry('"C:\\Other\\HarborVoice.exe"', 1),
        delete_value=lambda: deletes.append(False),
    )

    matching.set_enabled(False)
    unrelated.set_enabled(False)

    assert deletes == [True]


def test_enabling_startup_refuses_to_overwrite_unrelated_registration() -> None:
    writes: list[StartupEntry] = []
    registration = StartupRegistration(
        command='"C:\\Apps\\HarborVoice.exe"',
        read_entry=lambda: StartupEntry('"C:\\Other\\HarborVoice.exe"', 1),
        write_entry=writes.append,
    )

    with pytest.raises(RuntimeError, match="different command"):
        registration.set_enabled(True)

    assert writes == []


def test_restore_reinstates_exact_previous_value() -> None:
    writes: list[StartupEntry] = []
    previous = StartupEntry('"C:\\Legacy\\HarborVoice.exe" --legacy', 2)
    registration = StartupRegistration(
        command='"C:\\Apps\\HarborVoice.exe"',
        read_entry=lambda: StartupEntry('"C:\\Apps\\HarborVoice.exe"', 1),
        write_entry=writes.append,
    )

    registration.restore(previous)

    assert writes == [previous]


def test_startup_state_requires_exact_owned_command() -> None:
    command = '"C:\\Program Files\\Harbor Voice\\HarborVoice.exe"'

    assert StartupRegistration(
        command=command,
        read_entry=lambda: StartupEntry(command, 1),
    ).is_enabled()
    assert not StartupRegistration(
        command=command,
        read_entry=lambda: StartupEntry('"C:\\Other\\HarborVoice.exe"', 1),
    ).is_enabled()


def test_packaged_launch_command_matches_installer_quoting(
    monkeypatch,
) -> None:
    monkeypatch.setattr(harbor_voice.startup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        harbor_voice.startup.sys,
        "executable",
        "C:\\Users\\person\\AppData\\Local\\Programs\\HarborVoice\\HarborVoice.exe",
    )

    assert harbor_voice.startup.current_launch_command() == (
        '"C:\\Users\\person\\AppData\\Local\\Programs\\HarborVoice\\HarborVoice.exe"'
    )
