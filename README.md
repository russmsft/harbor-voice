# Harbor Voice

Harbor Voice is a personal, Windows-first voice assistant that lives in the system tray. Hold a global shortcut, speak, release, and hear a concise response. Audio transcription and speech stay on your computer by default; reasoning and project work use your existing local Codex installation and authentication.

This is an original implementation. It does not contain Backtalk source, assets, configuration, prompts, copy, or branding.

## Current capabilities

- Global hold-to-talk shortcut (`F9` by default).
- In-memory microphone capture; recordings are not saved to disk.
- Local transcription with faster-whisper (`base.en` by default).
- Local Windows SAPI speech with immediate interruption.
- One persistent Codex conversation in a selected working folder.
- Read-only normal turns with Codex approvals denied.
- One-shot confirmation for file edits, opening registered applications, opening HTTPS pages, and replacing clipboard text.
- Automatic read-only folder research and clipboard reading.
- System tray status, compact transcript window, settings, diagnostics, and single-instance protection.

Harbor Voice deliberately does not implement always-listening audio, deletion, unrestricted shell commands, credential access, background autonomy, email, calendar, messaging, or work outside the selected folder.

## Requirements

- Windows 10 or 11.
- Python 3.11 or 3.12 for development.
- [uv](https://docs.astral.sh/uv/) for development and builds.
- Codex installed and authenticated for live conversations. The integration follows the official [Codex SDK documentation](https://developers.openai.com/codex/sdk/).
- A working microphone and Windows SAPI voice.

The first local transcription run downloads the selected Whisper model. `base.en` is the default to keep the initial download and CPU latency modest.

## Development setup

```powershell
uv sync --group dev
uv run harbor-voice-doctor --json
uv run harbor-voice
```

On first launch, select the folder Harbor Voice may read. `F9` starts and stops capture. Pressing it while Harbor Voice is speaking stops playback before recording begins.

## Permissions

Normal Codex turns use a read-only sandbox and deny SDK approval requests. Harbor Voice owns a separate typed action layer:

| Action | Behaviour |
| --- | --- |
| General answers and web research | Automatic |
| Read inside the selected folder | Automatic |
| Write inside the selected folder | One visible approval, then one `workspace_write` turn |
| Read clipboard text | Automatic only when requested |
| Replace clipboard text | One visible approval |
| Open registered application | One visible approval |
| Open HTTPS page | One visible approval |
| Delete, arbitrary shell, credentials, outside-folder access | Blocked |

Approvals display the exact target, expire after five minutes, and cannot be reused.

## Privacy and local data

Settings and optional transcript history live under `%LOCALAPPDATA%\HarborVoice`. Transcript retention defaults to the current session only. Operational logs exclude recordings, prompts, responses, clipboard bodies, tokens, and secrets. Models and Codex credentials are not stored in the repository or installation directory.

## Diagnostics

Diagnostics are read-only: they check the runtime, Codex SDK, selected workspace, microphone, SAPI voice and hotkey library without authenticating, downloading, launching, writing, or changing settings.

```powershell
uv run harbor-voice-doctor --json
```

## Tests and build

```powershell
uv lock --check
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q src
.\packaging\build.ps1
```

The PyInstaller output is created at `dist\HarborVoice`. Install for the current user with:

```powershell
.\packaging\install.ps1 -Source .\dist\HarborVoice
```

Launch-at-login is off by default. Enable it only when desired:

```powershell
.\packaging\install.ps1 -Source .\dist\HarborVoice -EnableLaunchAtLogin
```

Uninstall while preserving settings and history:

```powershell
.\packaging\uninstall.ps1
```

Add `-RemoveUserData` only when the local settings and retained history should also be removed.

## Status

This is the first personal MVP. Live microphone, voice, Codex authentication, model download and packaged-app behaviour must be smoke-tested on the target Windows account before relying on it for daily use.

## License

Original Harbor Voice code is MIT licensed. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the major runtime components whose own licences continue to apply.

