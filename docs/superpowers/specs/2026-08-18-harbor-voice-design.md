# Harbor Voice Design Specification

> Provider update (2026-08-19): Harbor Voice now uses the GitHub Copilot CLI in
> restricted non-interactive mode instead of the original Codex SDK design. The
> implementation and README are authoritative for the current provider boundary;
> Codex-specific sections below are retained as historical design rationale. Specification

## Purpose

Harbor Voice is a personal, Windows-first voice assistant that lives in the system tray. It lets one user hold a global shortcut, speak a request, hear the response, and approve a small set of clearly described computer actions. It is an original implementation informed by common voice-assistant patterns; it does not reuse Backtalk source, assets, configuration, wording, identifiers, or branding.

The first release must be useful as a daily personal assistant while remaining deliberately constrained. It will answer general questions, perform web research through Codex, work with files in one user-approved folder, open an explicitly named application or website, and read or replace clipboard text. It will not operate continuously or act invisibly.

## Product principles

- Local-first audio: microphone recordings and speech synthesis remain on the computer by default.
- Push-to-talk only: the microphone is opened only while the configured shortcut is held.
- Visible agency: every state and every consequential action is visible to the user.
- Least privilege: reasoning begins in a read-only Codex sandbox. Writes and desktop actions require explicit approval.
- Interruptible: pressing push-to-talk while speech is playing stops speech immediately and starts a new turn.
- Replaceable providers: transcription, speech, reasoning, and desktop actions communicate through narrow interfaces.
- Personal, not autonomous: there are no unattended background jobs in the first release.

## MVP scope

### Included

- Windows system-tray application.
- Configurable global hold-to-talk shortcut.
- On-device transcription using a local speech-to-text provider.
- On-device speech using Windows SAPI through a speech-provider interface.
- One persistent Codex conversation using the official Python Codex SDK.
- One configured working folder for file-oriented tasks.
- General questions and Codex-backed web research.
- Read-only access inside the configured working folder without per-action approval.
- Approval-gated file writes, application launches, website launches, and clipboard changes.
- Compact conversation window containing the latest transcript, reply, state, and pending action.
- Settings for shortcut, microphone, speaker/voice, working folder, transcript retention, and launch-at-login preference. Launch at login is off by default.
- Local structured logs without recorded audio or secrets.
- Single-instance enforcement.

### Excluded

- Always-listening or wake-word operation.
- Email, calendar, messaging, smart-home, telephony, or account connectors.
- Autonomous recurring work and background monitoring.
- Arbitrary shell execution exposed as a desktop action.
- Credential, password-manager, browser-cookie, or protected-system access.
- Deletion of files or folders.
- Work outside the configured folder.
- Multiple simultaneous conversations.
- macOS, Linux, mobile, and web clients.
- Cloud speech services.

## User experience

### Tray states

The tray icon and conversation window expose exactly one of these states:

- `idle`: ready for input.
- `listening`: the shortcut is held and audio is being captured.
- `transcribing`: local transcription is running.
- `thinking`: Codex is processing the request.
- `approval`: a proposed action is waiting for the user.
- `speaking`: a response is being spoken.
- `muted`: microphone capture is disabled by the user.
- `error`: the last operation failed; selecting the tray item shows a recoverable explanation.

The tray menu contains Show conversation, Mute/Unmute, Settings, New conversation, and Quit. The conversation window is compact and can be closed without terminating the assistant.

### Turn flow

1. The user holds the global shortcut.
2. If speech is playing, playback is cancelled before capture begins.
3. Audio capture starts and the state becomes `listening`.
4. Releasing the shortcut ends capture. Recordings shorter than 250 milliseconds are ignored.
5. Local transcription produces text. Empty or low-confidence results return to `idle` with a short visual notice and no Codex turn.
6. The coordinator sends the text and current working-folder context to the persistent Codex thread in a read-only sandbox.
7. Codex returns either an informational response or an action proposal in a validated application-owned envelope.
8. Informational responses are displayed and spoken.
9. Safe read-only file research within the configured folder may complete automatically.
10. A gated action moves the app to `approval` and presents its type, exact target, effect, and editable user-facing description.
11. Reject returns to `idle` and records no approval. Approve executes only the displayed action and then speaks the result.
12. Starting another turn cancels pending speech. A pending approval remains visible until approved, rejected, or explicitly cancelled.

## Architecture

### Package layout

The Python package is divided by responsibility:

- `harbor_voice.app`: application composition and lifecycle.
- `harbor_voice.domain`: immutable requests, responses, actions, states, and errors.
- `harbor_voice.coordinator`: the turn state machine and cancellation rules.
- `harbor_voice.policy`: path containment and action authorization decisions.
- `harbor_voice.backends`: protocols plus Codex, transcription, and speech implementations.
- `harbor_voice.actions`: application, website, clipboard, and file-write executors.
- `harbor_voice.audio`: microphone capture and playback cancellation primitives.
- `harbor_voice.ui`: PySide6 tray icon, conversation window, approval dialog, and settings.
- `harbor_voice.storage`: validated settings, transcript retention, structured logging, and single-instance lock.

No UI module may import a concrete AI or audio provider. The coordinator depends only on protocols and domain types. Platform-specific effects are isolated behind action executors.

### Core interfaces

The design uses asynchronous protocols with cancellation:

```python
class Transcriber(Protocol):
    async def transcribe(self, recording: Recording) -> Transcript: ...

class AssistantBackend(Protocol):
    async def start(self, workspace: Path) -> None: ...
    async def ask(self, request: AssistantRequest) -> AssistantResponse: ...
    async def reset(self) -> None: ...
    async def close(self) -> None: ...

class Speaker(Protocol):
    async def speak(self, text: str) -> None: ...
    def cancel(self) -> None: ...

class ActionExecutor(Protocol):
    async def execute(self, action: ApprovedAction) -> ActionResult: ...
```

Concrete providers are composed only in the application entrypoint. Tests use in-memory fakes that implement the same protocols.

### Codex integration

The reasoning backend uses `openai-codex`, the official Python SDK, and maintains one local thread for the application session. The SDK supports persistent threads and explicit `read_only` and `workspace_write` sandbox presets.

Each initial turn runs with `Sandbox.read_only`. The system instruction requires one of two response shapes:

- `message`: user-facing prose with no requested desktop effect.
- `proposal`: a typed action proposal with an action identifier, target, summary, and optional response text.

Application code parses and validates the envelope. Unstructured or invalid output becomes a plain informational response and cannot execute an action.

For an approved file-edit request, the first read-only turn describes target files and intended effect. Approval permits a second turn on the same thread with `Sandbox.workspace_write` and an instruction containing the approved scope. The sandbox and an application-level path policy both restrict work to the configured folder. The assistant cannot request full-access mode.

Opening apps/websites and clipboard changes are application-owned typed actions. Codex may propose them, but only the local executor can perform them after approval.

## Permission policy

| Capability | MVP decision | Conditions |
| --- | --- | --- |
| General conversation | Automatic | No desktop effect |
| Web research | Automatic | Performed by the configured Codex environment; sources included in the response when available |
| Read files | Automatic | Resolved canonical path must remain inside the configured working folder |
| Write/edit files | Confirm | Show working folder, target paths, and intended effect; execute through `workspace_write` only |
| Open an application | Confirm | Executable must resolve from an allow-listed application registration; no arbitrary command line |
| Open a website | Confirm | `https` only; show normalized host and full URL |
| Read clipboard text | Automatic | Text only; maximum 100,000 characters; never automatically sent unless the user requests clipboard use |
| Replace clipboard text | Confirm | Show a preview and length; text only |
| Delete or move files | Blocked | Not implemented in MVP |
| Shell, PowerShell, registry, services | Blocked as desktop actions | Codex remains constrained by its sandbox for approved project work |
| Credentials and protected data | Blocked | No credential-store enumeration or secret extraction |
| Work outside configured folder | Blocked | Canonical containment check, including symlink/junction resolution |

Approval applies to one displayed action only and expires after five minutes or when the working folder changes. Rejection never causes the system to retry under a different action type.

## Settings and local data

Application data lives under `%LOCALAPPDATA%\HarborVoice`:

- `settings.json`: validated non-secret settings.
- `history.jsonl`: optional transcript history; disabled retention means completed turns are not persisted.
- `logs/harbor-voice.log`: rotating operational logs with state changes and error codes, but no audio, prompt bodies, response bodies, clipboard contents, or secrets.
- `models/`: optional downloaded transcription models or provider cache pointers.

Settings are written atomically through a temporary file and replacement. Invalid settings are quarantined and defaults are loaded with a visible warning. The configured working folder must exist and is stored as a resolved absolute path.

The MVP does not require an OpenAI API key file. The Codex SDK uses the local Codex runtime and its existing authentication. Future provider secrets must use Windows Credential Manager.

Transcript retention choices are session only, seven days, thirty days, or indefinitely. The default is session only. “New conversation” resets the Codex thread and clears the visible session; it does not delete retained history unless the user selects Clear history.

## Audio design

The default transcriber is `faster-whisper` using a configurable local model and CPU `int8` mode unless a supported GPU is selected. The initial default model is `base.en` to reduce first-run size and latency. The provider interface permits a multilingual model later.

Microphone capture uses 16 kHz mono PCM. Capture exists only between global shortcut press and release. The temporary recording is held in memory and is discarded after transcription; it is never written to disk by default.

The default speaker uses Windows SAPI through a dedicated worker so PySide6 remains responsive. Speech requests are queued by response segment and may be cancelled between or during segments. If speech fails, the response remains visible and the app returns to `idle` with an error indicator.

The first release does not attempt acoustic echo cancellation because push-to-talk closes playback before capture. It also avoids cloud TTS and model-specific voice assets.

## Error handling and recovery

- Missing or busy microphone: show device-specific guidance, keep the app running, and allow device reselection.
- Transcription model unavailable: expose download/load progress; failed downloads leave conversation disabled rather than falling back to cloud audio.
- Codex unavailable or unauthenticated: show a diagnostic with a sign-in/setup action; never reinterpret the request as a desktop action locally.
- Invalid assistant envelope: render safe prose when possible and block all effects.
- Approval-time target change: recompute policy immediately before execution; changed or escaped targets are blocked.
- Action failure: speak and display a concise failure; do not retry consequential effects automatically.
- Hotkey conflict: keep the tray available and direct the user to select another shortcut.
- Provider crash: cancel the current turn, return to `error`, and permit a new turn or provider restart without restarting Windows.
- Application crash: rotating logs enable diagnosis; no automatic recovery executes a pending action.

Errors are represented by stable application error codes and user-facing messages. Raw exceptions are logged without request content and are available only in the diagnostic view.

## Technology choices

- Python 3.11 or 3.12.
- `uv` for reproducible dependency management and a checked-in lockfile.
- PySide6 for the Windows tray, windows, notifications, event loop, and clipboard integration.
- `pynput` for the global hold-to-talk key listener.
- `sounddevice` and NumPy for microphone capture.
- `faster-whisper` for local transcription.
- Windows SAPI through `pyttsx3` for the default local voice.
- `openai-codex` for the persistent Codex backend.
- Pydantic v2 for settings and assistant-envelope validation.
- `platformdirs` for application-data paths.
- `pytest`, `pytest-asyncio`, and `pytest-qt` for automated tests.
- PyInstaller one-folder packaging for the first distributable build.

PySide6 is preferred over a split .NET/Python architecture for the MVP because one process can own the tray, dialogs, clipboard, lifecycle, and Python AI/audio providers. Platform interfaces preserve the option to replace the shell later.

The Codex integration follows the official [Codex SDK documentation](https://developers.openai.com/codex/sdk/). The local-first audio decision follows OpenAI's distinction between request-based/local pipelines and cloud [Realtime audio](https://developers.openai.com/api/docs/guides/realtime) sessions; Realtime remains a possible future provider rather than an MVP dependency.

## Testing strategy

All production behavior is developed test-first.

### Unit tests

- State-machine transitions and cancellation.
- Assistant-envelope validation and invalid-output safety.
- Canonical path containment, including prefix tricks and junction/symlink cases where supported.
- Permission decisions for every action type.
- Approval expiry and one-action semantics.
- Settings validation, quarantine, atomic writes, and defaults.
- Transcript-retention decisions and log redaction.
- URL normalization and application allow-list resolution.
- Speech segmentation and cancellation logic.

### Integration tests

- Full turn flow with fake transcriber, assistant, speaker, and executors.
- Read-only answer flow without approval.
- File-write proposal, approval, scoped execution, and result response.
- Rejection and expiry produce no side effect.
- A second push-to-talk event cancels speech before starting capture.
- PySide6 tray/window state reflects coordinator state using an offscreen Qt platform.

### Contract and manual tests

- Optional live Codex contract test behind an explicit marker and existing authentication.
- Windows microphone capture/release smoke test.
- Global hotkey press/release smoke test.
- SAPI voice and cancellation smoke test.
- Tray single-instance, notification, settings, and quit smoke test.
- Packaged application launch on a clean Windows user profile.

Live AI, microphone, and speaker tests are not part of the default automated suite. The automated suite must pass without network, audio hardware, or Codex credentials.

## Packaging and installation

Development runs through `uv run harbor-voice`. The repository includes a diagnostic command that checks Python/runtime version, Codex availability and authentication, microphone devices, SAPI voices, model availability, the working folder, and hotkey registration without performing assistant actions.

The first distributable is a signed-ready PyInstaller one-folder build with a visible installer script that:

- installs only under the current user,
- creates an optional Start Menu shortcut,
- does not enable launch at login unless selected,
- stores models and settings outside the installation folder,
- supports a normal uninstall without removing transcript history unless requested.

Packaging does not bundle Codex credentials. First run explains that Codex must already be installed and authenticated or guides the user to its official setup.

## Originality and licensing

Harbor Voice will be implemented from this specification with original module names, interfaces, prompts, copy, UX, tests, assets, and packaging. It may use independently licensed third-party libraries through their public APIs. No Backtalk source or bundled audio asset will be copied or adapted.

The initial repository license will be MIT for the original Harbor Voice code, subject to a dependency-license audit before public distribution. The personal build will include dependency notices and model-license information where required.

## Acceptance criteria

The MVP is ready for personal use when all of the following are true:

- It starts as one Windows tray process and remains available when the conversation window is closed.
- Holding and releasing the configured shortcut produces a local transcript without writing recorded audio to disk.
- A general request receives a visible and spoken response through a persistent Codex thread.
- Starting a new turn stops current speech before microphone capture begins.
- Read-only project questions work inside the selected folder.
- Every file write, application launch, website launch, and clipboard replacement requires one visible, unexpired approval.
- Blocked actions cannot be executed through malformed model output, path escaping, or approval reuse.
- Settings and transcript retention behave as documented.
- The default automated test suite passes without credentials, network, microphone, or speaker.
- The packaged build can be installed, launched, diagnosed, and uninstalled by the current Windows user.
