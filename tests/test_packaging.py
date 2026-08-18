from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.fixture
def fake_distribution(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "HarborVoice.exe").write_bytes(b"assistant")
    (source / "HarborVoiceDoctor.exe").write_bytes(b"doctor")
    return source


def run_script(
    script: str,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if POWERSHELL is None:
        pytest.skip("PowerShell is required on the Windows target")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, *arguments],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


def test_installer_copies_distribution_and_creates_shortcut_without_autostart(
    tmp_path: Path,
    fake_distribution: Path,
) -> None:
    install_root = tmp_path / "install"
    menu_root = tmp_path / "menu"

    run_script(
        "packaging/install.ps1",
        "-Source",
        str(fake_distribution),
        "-InstallRoot",
        str(install_root),
        "-StartMenuRoot",
        str(menu_root),
    )

    assert (install_root / "HarborVoice.exe").read_bytes() == b"assistant"
    assert (install_root / "HarborVoiceDoctor.exe").read_bytes() == b"doctor"
    assert (install_root / ".harbor-voice-installation").is_file()
    assert (menu_root / "Harbor Voice.lnk").exists()
    assert (menu_root / ".harbor-voice-start-menu").is_file()


def test_installer_refuses_nonempty_unowned_destination(
    tmp_path: Path,
    fake_distribution: Path,
) -> None:
    install_root = tmp_path / "shared-tools"
    menu_root = tmp_path / "menu"
    install_root.mkdir()
    unrelated = install_root / "unrelated.exe"
    unrelated.write_bytes(b"keep")

    result = run_script(
        "packaging/install.ps1",
        "-Source",
        str(fake_distribution),
        "-InstallRoot",
        str(install_root),
        "-StartMenuRoot",
        str(menu_root),
        check=False,
    )

    assert result.returncode != 0
    assert unrelated.read_bytes() == b"keep"
    assert not (install_root / ".harbor-voice-installation").exists()


def test_uninstaller_preserves_user_data_unless_explicitly_requested(
    tmp_path: Path,
    fake_distribution: Path,
) -> None:
    install_root = tmp_path / "install"
    menu_root = tmp_path / "menu"
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "history.jsonl").write_text("private", encoding="utf-8")
    (data_root / ".harbor-voice-data").write_text("owned", encoding="utf-8")
    run_script(
        "packaging/install.ps1",
        "-Source",
        str(fake_distribution),
        "-InstallRoot",
        str(install_root),
        "-StartMenuRoot",
        str(menu_root),
    )

    run_script(
        "packaging/uninstall.ps1",
        "-InstallRoot",
        str(install_root),
        "-StartMenuRoot",
        str(menu_root),
        "-DataRoot",
        str(data_root),
    )

    assert not install_root.exists()
    assert data_root.exists()
    run_script(
        "packaging/uninstall.ps1",
        "-InstallRoot",
        str(install_root),
        "-StartMenuRoot",
        str(menu_root),
        "-DataRoot",
        str(data_root),
        "-RemoveUserData",
    )
    assert not data_root.exists()


def test_uninstaller_refuses_unmarked_custom_data_directory(tmp_path: Path) -> None:
    install_root = tmp_path / "missing-install"
    menu_root = tmp_path / "missing-menu"
    documents = tmp_path / "documents"
    documents.mkdir()
    important = documents / "important.txt"
    important.write_text("keep", encoding="utf-8")

    result = run_script(
        "packaging/uninstall.ps1",
        "-InstallRoot",
        str(install_root),
        "-StartMenuRoot",
        str(menu_root),
        "-DataRoot",
        str(documents),
        "-RemoveUserData",
        check=False,
    )

    assert result.returncode != 0
    assert important.read_text(encoding="utf-8") == "keep"
