from pathlib import Path

from PyInstaller.utils.hooks import collect_all

project_root = Path.cwd()
pathex = [str(project_root / "src")]
datas = []
binaries = []
hiddenimports = []
for package in (
    "ctranslate2",
    "faster_whisper",
    "openai_codex",
    "pyttsx3",
    "sounddevice",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

app_analysis = Analysis(
    [str(project_root / "src" / "harbor_voice" / "app.py")],
    pathex=pathex,
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
app_pyz = PYZ(app_analysis.pure)
app_exe = EXE(
    app_pyz,
    app_analysis.scripts,
    [],
    exclude_binaries=True,
    name="HarborVoice",
    console=False,
    disable_windowed_traceback=False,
)

doctor_analysis = Analysis(
    [str(project_root / "src" / "harbor_voice" / "diagnostics.py")],
    pathex=pathex,
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
doctor_pyz = PYZ(doctor_analysis.pure)
doctor_exe = EXE(
    doctor_pyz,
    doctor_analysis.scripts,
    [],
    exclude_binaries=True,
    name="HarborVoiceDoctor",
    console=True,
    disable_windowed_traceback=False,
)

collection = COLLECT(
    app_exe,
    doctor_exe,
    app_analysis.binaries,
    app_analysis.datas,
    doctor_analysis.binaries,
    doctor_analysis.datas,
    strip=False,
    upx=True,
    name="HarborVoice",
)

