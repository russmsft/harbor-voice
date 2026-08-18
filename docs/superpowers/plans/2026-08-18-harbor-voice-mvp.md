# Harbor Voice MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows system-tray personal voice assistant with local push-to-talk audio, a persistent read-only-by-default Codex thread, and one-shot approval for a small set of desktop actions.

**Architecture:** A PySide6 tray shell composes testable ports for recording, transcription, reasoning, speech, policy, and effects. The coordinator owns the state machine and never performs operating-system effects directly. Codex runs in `Sandbox.read_only` for normal turns and receives `Sandbox.workspace_write` only after a matching, unexpired file-write approval.

**Tech Stack:** Python 3.11/3.12, uv, PySide6, qasync, openai-codex, Pydantic v2, pynput, sounddevice, NumPy, faster-whisper, pyttsx3, platformdirs, pytest, pytest-asyncio, pytest-qt, Ruff, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-08-18-harbor-voice-design.md`

## Global Constraints

- Windows 10/11 is the only supported runtime for the MVP.
- Microphone audio stays in memory and is never written to disk by default.
- The microphone opens only while the configured push-to-talk shortcut is held.
- Codex starts every normal turn with `Sandbox.read_only` and `ApprovalMode.deny_all`.
- The application never requests `Sandbox.full_access`.
- File writes, app launches, HTTPS launches, and clipboard replacement require one visible approval that expires after five minutes.
- Deletion, arbitrary shell execution, credentials, protected data, and paths outside the configured workspace are blocked.
- Default automated tests require no network, credentials, microphone, speaker, or GUI display.
- Harbor Voice code, copy, prompts, tests, and assets remain original and do not copy Backtalk.
- Every production behavior follows red-green-refactor; watch each new test fail for the expected missing behavior before adding implementation.

## File map

- `pyproject.toml`: package metadata, dependencies, entrypoints, and tool configuration.
- `src/harbor_voice/domain.py`: immutable states, recordings, transcripts, responses, actions, and results.
- `src/harbor_voice/ports.py`: protocols used by the coordinator.
- `src/harbor_voice/policy.py`: canonical path, URL, application, expiry, and authorization rules.
- `src/harbor_voice/coordinator.py`: turn lifecycle, cancellation, pending approval, and state publication.
- `src/harbor_voice/storage.py`: validated settings, atomic persistence, retention, logging, and app paths.
- `src/harbor_voice/audio/capture.py`: in-memory microphone capture.
- `src/harbor_voice/audio/hotkey.py`: global hold-key press/release adapter.
- `src/harbor_voice/backends/transcription.py`: faster-whisper adapter.
- `src/harbor_voice/backends/speech.py`: cancellable Windows SAPI adapter.
- `src/harbor_voice/backends/codex.py`: structured Codex thread and approved workspace-write turn.
- `src/harbor_voice/actions.py`: typed local effect executors and router.
- `src/harbor_voice/ui/tray.py`: tray icon, status menu, and single-instance guard.
- `src/harbor_voice/ui/window.py`: conversation, approval, and settings windows.
- `src/harbor_voice/app.py`: dependency composition and Qt/async lifecycle.
- `src/harbor_voice/diagnostics.py`: read-only environment checks.
- `tests/`: matching unit, integration, and offscreen Qt tests.
- `packaging/`: PyInstaller spec and current-user install/uninstall scripts.

---

### Task 1: Package foundation and domain contract

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `LICENSE`
- Create: `src/harbor_voice/__init__.py`
- Create: `src/harbor_voice/domain.py`
- Create: `tests/conftest.py`
- Create: `tests/test_domain.py`

**Interfaces:**
- Produces: `AppState`, `AudioRecording`, `Transcript`, `AssistantRequest`, `MessageResponse`, `ProposalResponse`, `ActionProposal`, `ActionKind`, `ActionResult`, and `parse_assistant_response(raw: str)`.

- [ ] **Step 1: Add package metadata and test tooling**

Create `pyproject.toml` with this structure, then add an MIT `LICENSE`, Python/editor/build ignores, and an empty package initializer:

```toml
[project]
name = "harbor-voice"
version = "0.1.0"
requires-python = ">=3.11,<3.13"
dependencies = [
  "faster-whisper", "numpy>=1.26", "openai-codex", "platformdirs>=4",
  "pydantic>=2.12", "pynput>=1.8", "PySide6>=6.8", "pyttsx3>=2.98",
  "qasync>=0.27", "sounddevice>=0.5",
]

[project.scripts]
harbor-voice = "harbor_voice.app:main"
harbor-voice-doctor = "harbor_voice.diagnostics:main"

[dependency-groups]
dev = ["pyinstaller>=6", "pytest>=8", "pytest-asyncio>=1", "pytest-qt>=4", "ruff>=0.15"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/harbor_voice"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
qt_api = "pyside6"

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM"]
```

Set Qt offscreen before pytest imports PySide:

```python
# tests/conftest.py
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
```

- [ ] **Step 2: Write the failing domain tests**

```python
def test_parse_message_response() -> None:
    response = parse_assistant_response('{"kind":"message","message":"Hello"}')
    assert isinstance(response, MessageResponse)
    assert response.message == "Hello"

def test_proposal_requires_action() -> None:
    with pytest.raises(ValidationError):
        parse_assistant_response('{"kind":"proposal","message":"Approve this"}')

def test_recording_rejects_non_mono_audio() -> None:
    with pytest.raises(ValueError, match="mono"):
        AudioRecording(pcm=b"\x00\x00", sample_rate=16_000, channels=2)
```

- [ ] **Step 3: Run the domain tests and verify RED**

Run: `uv run pytest tests/test_domain.py -v`

Expected: collection fails because `harbor_voice.domain` does not exist.

- [ ] **Step 4: Implement the domain models**

Use string enums for the eight app states and five proposal kinds (`file_write`, `open_app`, `open_url`, `clipboard_read`, `clipboard_replace`). Use frozen Pydantic models for proposals/responses and frozen dataclasses for audio/transcripts/results. Parse the discriminated response union with `TypeAdapter(MessageResponse | ProposalResponse)` and reject extra fields.

```python
def parse_assistant_response(raw: str) -> AssistantResponse:
    return _RESPONSE_ADAPTER.validate_json(raw)
```

- [ ] **Step 5: Verify GREEN and lock dependencies**

Run: `uv lock && uv run pytest tests/test_domain.py -v && uv run ruff check .`

Expected: all domain tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit**

```powershell
git add pyproject.toml uv.lock .gitignore LICENSE src tests/conftest.py tests/test_domain.py
git commit -m "feat: establish Harbor Voice domain contract"
```

---

### Task 2: Permission policy and containment

**Files:**
- Create: `src/harbor_voice/policy.py`
- Create: `tests/test_policy.py`

**Interfaces:**
- Consumes: `ActionProposal`, `ActionKind`.
- Produces: `Disposition`, `PolicyDecision`, `PermissionPolicy.evaluate(action, now)`, and `PermissionPolicy.revalidate(action, now)`.

- [ ] **Step 1: Write policy tests first**

```python
@pytest.mark.parametrize("name", ["notes.txt", "folder/report.md"])
def test_workspace_file_write_requires_confirmation(tmp_path: Path, name: str) -> None:
    policy = PermissionPolicy(tmp_path, registered_apps={})
    action = proposal(ActionKind.FILE_WRITE, str(tmp_path / name))
    assert policy.evaluate(action, now=100).disposition is Disposition.CONFIRM

def test_prefix_escape_is_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    action = proposal(ActionKind.FILE_WRITE, str(tmp_path / "work-secret" / "x.txt"))
    assert PermissionPolicy(workspace, {}).evaluate(action, 100).disposition is Disposition.BLOCK

def test_only_https_url_can_be_confirmed(tmp_path: Path) -> None:
    policy = PermissionPolicy(tmp_path, {})
    assert policy.evaluate(proposal(ActionKind.OPEN_URL, "https://example.com/a"), 100).disposition is Disposition.CONFIRM
    assert policy.evaluate(proposal(ActionKind.OPEN_URL, "file:///C:/Windows/win.ini"), 100).disposition is Disposition.BLOCK
```

Also cover URL credentials, unknown applications, registered application resolution, clipboard size, missing workspace, and approval older than 300 seconds.

- [ ] **Step 2: Run policy tests and verify RED**

Run: `uv run pytest tests/test_policy.py -v`

Expected: import failure for `harbor_voice.policy`.

- [ ] **Step 3: Implement minimal policy rules**

Canonicalize workspace and targets with `Path.resolve(strict=False)` and use `target.is_relative_to(workspace)`; never use string-prefix checks. Normalize URLs with `urllib.parse.urlsplit`, require `https`, a hostname, and no embedded username/password. Resolve app names only through the supplied allow-list. Return stable reason codes such as `outside_workspace`, `scheme_blocked`, and `app_not_registered`.

```python
def _inside(root: Path, candidate: str) -> bool:
    target = Path(candidate).expanduser().resolve(strict=False)
    return target == root or target.is_relative_to(root)

def _safe_https(target: str) -> bool:
    parsed = urlsplit(target)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
```

- [ ] **Step 4: Verify policy GREEN**

Run: `uv run pytest tests/test_policy.py -v && uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/harbor_voice/policy.py tests/test_policy.py
git commit -m "feat: enforce one-shot assistant permissions"
```

---

### Task 3: Ports and turn coordinator

**Files:**
- Create: `src/harbor_voice/ports.py`
- Create: `src/harbor_voice/coordinator.py`
- Create: `tests/fakes.py`
- Create: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: domain models and `PermissionPolicy`.
- Produces: `Transcriber`, `AssistantBackend`, `Speaker`, `ActionRunner`, `StateSink` protocols and `TurnCoordinator.submit(recording)`, `.approve(action_id)`, `.reject(action_id)`, `.cancel_speech()`, `.new_conversation()`.

- [ ] **Step 1: Write the coordinator’s failing integration tests**

```python
async def test_message_turn_transcribes_asks_and_speaks(rig: Rig) -> None:
    rig.transcriber.result = Transcript("What time is it?", confidence=0.9)
    rig.backend.response = MessageResponse(kind="message", message="It is noon.")
    await rig.coordinator.submit(sample_recording())
    assert rig.backend.requests[0].text == "What time is it?"
    assert rig.speaker.spoken == ["It is noon."]
    assert rig.states == [AppState.TRANSCRIBING, AppState.THINKING, AppState.SPEAKING, AppState.IDLE]

async def test_confirmation_has_no_effect_before_approval(rig: Rig) -> None:
    rig.backend.response = proposal_response(ActionKind.OPEN_URL, "https://example.com")
    await rig.coordinator.submit(sample_recording())
    assert rig.runner.executed == []
    assert rig.states[-1] is AppState.APPROVAL

async def test_new_turn_cancels_speech_before_transcription(rig: Rig) -> None:
    rig.speaker.active = True
    await rig.coordinator.submit(sample_recording())
    assert rig.events.index("speaker.cancel") < rig.events.index("transcriber.transcribe")
```

Add rejection, expired approval, mismatched action ID, blocked proposal, empty transcript, action failure, and revalidation-before-effect tests.

- [ ] **Step 2: Run coordinator tests and verify RED**

Run: `uv run pytest tests/test_coordinator.py -v`

Expected: missing coordinator and ports modules.

- [ ] **Step 3: Implement protocols and state machine**

Store at most one `PendingApproval(action, created_at)`. Publish state synchronously through the injected sink. Re-evaluate policy during `approve`; clear the pending record before awaiting the effect to prevent double execution. Wrap provider failures as stable `AssistantError` values, publish `ERROR`, and make `recover()` return to `IDLE` without replaying work.

```python
async def approve(self, action_id: UUID) -> None:
    pending, self._pending = self._pending, None
    if pending is None or pending.action.id != action_id:
        raise ApprovalNotFound(str(action_id))
    decision = self._policy.revalidate(pending.action, self._clock())
    if decision.disposition is not Disposition.CONFIRM:
        raise ApprovalExpired(decision.reason)
    result = await self._runner.execute(pending.action)
    await self._speak_result(result)
```

- [ ] **Step 4: Verify coordinator GREEN**

Run: `uv run pytest tests/test_coordinator.py -v && uv run pytest -q`

Expected: all tests pass with no warnings.

- [ ] **Step 5: Commit**

```powershell
git add src/harbor_voice/ports.py src/harbor_voice/coordinator.py tests/fakes.py tests/test_coordinator.py
git commit -m "feat: coordinate safe interruptible voice turns"
```

---

### Task 4: Settings, history, logging, and local paths

**Files:**
- Create: `src/harbor_voice/storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Produces: `AssistantSettings`, `Retention`, `AppPaths`, `SettingsStore`, `HistoryStore`, `configure_logging(paths)`.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_default_retention_is_session_only(tmp_path: Path) -> None:
    settings = SettingsStore(tmp_path / "settings.json").load()
    assert settings.retention is Retention.SESSION
    assert settings.launch_at_login is False

def test_settings_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    replacements: list[tuple[Path, Path]] = []
    monkeypatch.setattr(storage.os, "replace", lambda a, b: replacements.append((Path(a), Path(b))))
    store.save(AssistantSettings(workspace=tmp_path))
    assert replacements == [(tmp_path / "settings.json.tmp", tmp_path / "settings.json")]

def test_session_history_does_not_touch_disk(tmp_path: Path) -> None:
    HistoryStore(tmp_path / "history.jsonl", Retention.SESSION).append(turn("hello"))
    assert not (tmp_path / "history.jsonl").exists()
```

Add invalid-settings quarantine, missing workspace, retention pruning, and log-redaction tests.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_storage.py -v`

Expected: missing `harbor_voice.storage`.

- [ ] **Step 3: Implement storage**

Use `platformdirs.user_data_path("HarborVoice")`. Validate settings with Pydantic, serialize with UTF-8, flush and `os.fsync`, then `os.replace`. Rename invalid JSON to `settings.invalid-YYYYMMDD-HHMMSS.json`. Store only completed text turns when retention is not session-only. Use `RotatingFileHandler(maxBytes=1_000_000, backupCount=3)` and a logging filter that excludes fields named `audio`, `prompt`, `response`, `clipboard`, `token`, or `secret`.

```python
def save(self, settings: AssistantSettings) -> None:
    temporary = self.path.with_suffix(self.path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(settings.model_dump_json(indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, self.path)
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `uv run pytest tests/test_storage.py -v && uv run pytest -q`

```powershell
git add src/harbor_voice/storage.py tests/test_storage.py
git commit -m "feat: persist private assistant settings safely"
```

---

### Task 5: In-memory recording, hotkey, and local transcription

**Files:**
- Create: `src/harbor_voice/audio/__init__.py`
- Create: `src/harbor_voice/audio/capture.py`
- Create: `src/harbor_voice/audio/hotkey.py`
- Create: `src/harbor_voice/backends/__init__.py`
- Create: `src/harbor_voice/backends/transcription.py`
- Create: `tests/test_audio.py`
- Create: `tests/test_transcription.py`

**Interfaces:**
- Produces: `MemoryRecorder.start()`, `.stop() -> AudioRecording | None`, `GlobalHoldKey`, and `FasterWhisperTranscriber.transcribe(recording)`.

- [ ] **Step 1: Write failing audio tests**

```python
def test_short_recording_is_ignored(fake_stream_factory: FakeStreamFactory) -> None:
    recorder = MemoryRecorder(fake_stream_factory, min_duration_ms=250)
    recorder.start(); fake_stream_factory.feed(pcm_ms(200)); result = recorder.stop()
    assert result is None

def test_audio_never_creates_a_file(tmp_path: Path, fake_stream_factory: FakeStreamFactory) -> None:
    before = set(tmp_path.iterdir())
    recorder = MemoryRecorder(fake_stream_factory, min_duration_ms=1)
    recorder.start(); fake_stream_factory.feed(pcm_ms(30)); recorder.stop()
    assert set(tmp_path.iterdir()) == before

async def test_transcriber_converts_pcm_and_strips_empty_text(fake_whisper: FakeWhisper) -> None:
    transcriber = FasterWhisperTranscriber(lambda: fake_whisper)
    fake_whisper.segments = [Segment("  hello ")]
    assert (await transcriber.transcribe(sample_recording())).text == "hello"
```

Test press/release de-duplication and ensure hotkey callbacks never perform audio or UI work on the listener thread.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_audio.py tests/test_transcription.py -v`

Expected: missing modules.

- [ ] **Step 3: Implement adapters**

Use `sounddevice.RawInputStream(samplerate=16000, channels=1, dtype="int16")` with a callback that extends a locked `bytearray`. `stop` closes the stream, copies bytes to `AudioRecording`, clears the buffer, and never writes a temporary file. Run faster-whisper work with `asyncio.to_thread`; initialize `WhisperModel(model_name, device=device, compute_type=compute_type)` lazily under a lock. The hotkey adapter emits thread-safe press/release callbacks exactly once per physical hold.

```python
def stop(self) -> AudioRecording | None:
    stream, self._stream = self._stream, None
    if stream is None:
        return None
    stream.stop(); stream.close()
    with self._lock:
        pcm, self._pcm = bytes(self._pcm), bytearray()
    duration_ms = len(pcm) * 1000 // (16_000 * 2)
    return None if duration_ms < self.min_duration_ms else AudioRecording(pcm, 16_000, 1)
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `uv run pytest tests/test_audio.py tests/test_transcription.py -v && uv run pytest -q`

```powershell
git add src/harbor_voice/audio src/harbor_voice/backends tests/test_audio.py tests/test_transcription.py
git commit -m "feat: capture and transcribe push-to-talk audio locally"
```

---

### Task 6: Cancellable Windows speech

**Files:**
- Create: `src/harbor_voice/backends/speech.py`
- Create: `tests/test_speech.py`

**Interfaces:**
- Produces: `split_for_speech(text, max_chars=320)` and `SapiSpeaker.speak(text)`, `.cancel()`, `.close()`.

- [ ] **Step 1: Write failing speech tests**

```python
def test_split_preserves_sentence_order() -> None:
    assert split_for_speech("One. Two? Three!") == ["One.", "Two?", "Three!"]

async def test_cancel_stops_engine_and_discards_queued_segments(fake_engine: FakeSapiEngine) -> None:
    speaker = SapiSpeaker(lambda: fake_engine)
    task = asyncio.create_task(speaker.speak("One. Two."))
    await fake_engine.started.wait(); speaker.cancel(); await task
    assert fake_engine.stop_calls == 1
    assert "Two." not in fake_engine.completed
```

- [ ] **Step 2: Verify RED, implement, and verify GREEN**

Run RED: `uv run pytest tests/test_speech.py -v`

Implement a dedicated worker owning one `pyttsx3` engine. Bridge completion to the asyncio loop with `loop.call_soon_threadsafe`. Split on sentence boundaries, cap segments at 320 characters, and use a generation counter so cancelled queued segments cannot resume after `engine.stop()`.

```python
def cancel(self) -> None:
    with self._lock:
        self._generation += 1
    self._commands.put(("stop", None, self._generation))

async def speak(self, text: str) -> None:
    generation = self._generation
    for segment in split_for_speech(text):
        if generation != self._generation:
            return
        await self._speak_one(segment, generation)
```

Run GREEN: `uv run pytest tests/test_speech.py -v && uv run pytest -q`

- [ ] **Step 3: Commit**

```powershell
git add src/harbor_voice/backends/speech.py tests/test_speech.py
git commit -m "feat: speak responses through cancellable Windows SAPI"
```

---

### Task 7: Structured Codex backend

**Files:**
- Create: `src/harbor_voice/backends/codex.py`
- Create: `tests/test_codex_backend.py`

**Interfaces:**
- Consumes: assistant response schema and workspace policy.
- Produces: `CodexBackend.start(workspace)`, `.ask(request)`, `.apply_workspace_change(action)`, `.reset()`, `.close()`.

- [ ] **Step 1: Write failing SDK-boundary tests**

```python
async def test_normal_turn_is_read_only(fake_codex: FakeCodex) -> None:
    backend = CodexBackend(codex_factory=lambda: fake_codex)
    await backend.start(Path("C:/work"))
    await backend.ask(AssistantRequest(text="Summarise this folder"))
    assert fake_codex.start_kwargs["sandbox"] is Sandbox.read_only
    assert fake_codex.start_kwargs["approval_mode"] is ApprovalMode.deny_all
    assert fake_codex.thread.run_calls[0].kwargs["sandbox"] is Sandbox.read_only

async def test_invalid_structured_output_cannot_become_action(fake_codex: FakeCodex) -> None:
    fake_codex.thread.final_response = "open powershell and delete files"
    response = await started_backend(fake_codex).ask(AssistantRequest(text="hello"))
    assert isinstance(response, MessageResponse)
    assert "safely interpret" in response.message

async def test_approved_file_change_uses_workspace_write(fake_codex: FakeCodex) -> None:
    await started_backend(fake_codex).apply_workspace_change(file_write_proposal("C:/work/a.txt"))
    assert fake_codex.thread.run_calls[-1].kwargs["sandbox"] is Sandbox.workspace_write
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_codex_backend.py -v`

Expected: missing backend module.

- [ ] **Step 3: Implement the official SDK adapter**

Late-import `AsyncCodex`, `ApprovalMode`, and `Sandbox`. Enter the async client context, call `thread_start(cwd=str(workspace), sandbox=Sandbox.read_only, approval_mode=ApprovalMode.deny_all, developer_instructions=VOICE_INSTRUCTIONS)`, and reuse the thread. Pass the Pydantic response JSON schema as `output_schema` on read-only turns. Parse `TurnResult.final_response`; any validation failure returns the fixed safe message without an action. The approved edit method includes the approved action ID, resolved targets, and summary and passes `sandbox=Sandbox.workspace_write`, never `full_access`.

```python
self._client = self._codex_factory()
await self._client.__aenter__()
self._thread = await self._client.thread_start(
    cwd=str(self.workspace),
    sandbox=Sandbox.read_only,
    approval_mode=ApprovalMode.deny_all,
    developer_instructions=VOICE_INSTRUCTIONS,
)
result = await self._thread.run(
    request.text,
    sandbox=Sandbox.read_only,
    approval_mode=ApprovalMode.deny_all,
    output_schema=ASSISTANT_RESPONSE_SCHEMA,
)
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `uv run pytest tests/test_codex_backend.py -v && uv run pytest -q`

```powershell
git add src/harbor_voice/backends/codex.py tests/test_codex_backend.py
git commit -m "feat: connect persistent sandboxed Codex conversations"
```

---

### Task 8: Approval-gated action executors

**Files:**
- Create: `src/harbor_voice/actions.py`
- Create: `tests/test_actions.py`

**Interfaces:**
- Produces: `ActionRouter.execute(action)`, `OpenApplicationExecutor`, `OpenUrlExecutor`, `ClipboardExecutor`, and `WorkspaceWriteExecutor`.

- [ ] **Step 1: Write failing executor tests**

```python
async def test_app_executor_uses_registered_path_without_shell(fake_start: FakeStart) -> None:
    executor = OpenApplicationExecutor({"notepad": Path("C:/Windows/notepad.exe")}, fake_start)
    await executor.execute(open_app("notepad"))
    assert fake_start.calls == [(Path("C:/Windows/notepad.exe"),)]

async def test_url_executor_receives_normalized_https_url(fake_browser: FakeBrowser) -> None:
    await OpenUrlExecutor(fake_browser).execute(open_url("https://example.com/path"))
    assert fake_browser.urls == ["https://example.com/path"]

async def test_router_has_no_generic_shell_fallback() -> None:
    with pytest.raises(UnsupportedAction):
        await ActionRouter({}).execute(unknown_action())
```

- [ ] **Step 2: Verify RED, implement typed effects, and verify GREEN**

Run RED: `uv run pytest tests/test_actions.py -v`

Use `subprocess.Popen([resolved_executable], shell=False)` only for registered apps, `webbrowser.open_new_tab` only for policy-normalized HTTPS URLs, the injected PySide clipboard port for text, and `CodexBackend.apply_workspace_change` for file edits. The router maps exact `ActionKind` values and raises for everything else.

```python
async def execute(self, action: ActionProposal) -> ActionResult:
    executable = self._registered[action.target.casefold()]
    self._start([str(executable)], shell=False)
    return ActionResult(success=True, message=f"Opened {action.target}.")
```

Run GREEN: `uv run pytest tests/test_actions.py -v && uv run pytest -q`

- [ ] **Step 3: Commit**

```powershell
git add src/harbor_voice/actions.py tests/test_actions.py
git commit -m "feat: execute only typed approved desktop actions"
```

---

### Task 9: PySide6 tray, windows, and approval UX

**Files:**
- Create: `src/harbor_voice/ui/__init__.py`
- Create: `src/harbor_voice/ui/tray.py`
- Create: `src/harbor_voice/ui/window.py`
- Create: `tests/test_ui.py`

**Interfaces:**
- Produces: `TrayController`, `ConversationWindow`, `ApprovalDialog`, `SettingsDialog`, and `SingleInstanceGuard`.

- [ ] **Step 1: Write offscreen Qt tests first**

```python
def test_tray_state_updates_tooltip(qtbot: QtBot) -> None:
    tray = TrayController()
    tray.set_state(AppState.LISTENING)
    assert tray.icon.toolTip() == "Harbor Voice — listening"

def test_approval_dialog_shows_exact_target(qtbot: QtBot) -> None:
    dialog = ApprovalDialog(open_url("https://example.com/private"))
    qtbot.addWidget(dialog)
    assert "https://example.com/private" in dialog.target_label.text()

def test_close_hides_conversation_without_quitting(qtbot: QtBot) -> None:
    window = ConversationWindow(); qtbot.addWidget(window); window.show(); window.close()
    assert window.isHidden()
```

Test every state label, approve/reject signals, disabled approval after expiry, settings validation, and tray menu actions.

- [ ] **Step 2: Verify RED, implement UI, and verify GREEN**

Run RED with `QT_QPA_PLATFORM=offscreen`: `uv run pytest tests/test_ui.py -v`

Generate the tray icon in code with `QPainter` so no copied asset is needed. Make windows presentation-only: they emit Qt signals and receive view models. Use `QLockFile` under the app-data folder for the single-instance guard. Never execute an action in a button handler; approval signals call the coordinator.

```python
class ApprovalDialog(QDialog):
    approved = Signal(UUID)
    rejected = Signal(UUID)

    def _approve_clicked(self) -> None:
        self.approved.emit(self.action_id)
        self.close()
```

Run GREEN: `uv run pytest tests/test_ui.py -v && uv run pytest -q`

- [ ] **Step 3: Commit**

```powershell
git add src/harbor_voice/ui tests/test_ui.py
git commit -m "feat: add visible Windows tray and approval interface"
```

---

### Task 10: Application composition and diagnostics

**Files:**
- Create: `src/harbor_voice/app.py`
- Create: `src/harbor_voice/diagnostics.py`
- Create: `tests/test_app.py`
- Create: `tests/test_diagnostics.py`

**Interfaces:**
- Produces: CLI entrypoints `harbor-voice` and `harbor-voice-doctor`.

- [ ] **Step 1: Write failing composition tests**

```python
def test_components_use_configured_workspace(tmp_path: Path) -> None:
    graph = build_components(settings(workspace=tmp_path), providers=fake_providers())
    assert graph.policy.workspace == tmp_path.resolve()
    assert graph.backend.workspace == tmp_path.resolve()

def test_doctor_is_read_only(fake_checks: FakeChecks) -> None:
    report = run_diagnostics(fake_checks)
    assert report.names == ["runtime", "codex", "workspace", "microphone", "voice", "hotkey"]
    assert fake_checks.effects == []
```

Add tests for hotkey thread-to-Qt dispatch, quit cleanup order, missing workspace setup state, and single-instance exit.

- [ ] **Step 2: Verify RED, implement composition, and verify GREEN**

Run RED: `uv run pytest tests/test_app.py tests/test_diagnostics.py -v`

Use `qasync.QEventLoop` as the Qt asyncio loop. A QObject signal bridge moves hotkey callbacks onto the UI thread; press cancels speech then starts recording, and release stops recording and schedules `coordinator.submit`. Close providers in the order hotkey, recorder, speaker, backend, tray. Diagnostics report status and guidance but never download, authenticate, launch, write, or change settings.

```python
def main() -> int:
    application = QApplication(sys.argv)
    loop = qasync.QEventLoop(application)
    asyncio.set_event_loop(loop)
    runtime = build_runtime(application, loop)
    with loop:
        loop.create_task(runtime.start())
        loop.run_forever()
    return 0
```

Run GREEN: `uv run pytest tests/test_app.py tests/test_diagnostics.py -v && uv run pytest -q`

- [ ] **Step 3: Commit**

```powershell
git add src/harbor_voice/app.py src/harbor_voice/diagnostics.py tests/test_app.py tests/test_diagnostics.py
git commit -m "feat: compose the Harbor Voice desktop application"
```

---

### Task 11: Packaging, installation, and user documentation

**Files:**
- Create: `README.md`
- Create: `packaging/harbor-voice.spec`
- Create: `packaging/install.ps1`
- Create: `packaging/uninstall.ps1`
- Create: `tests/test_packaging.py`

**Interfaces:**
- Produces: development, diagnostic, build, current-user install, and uninstall workflows.

- [ ] **Step 1: Write packaging contract tests**

```python
def test_installer_does_not_enable_autostart_by_default() -> None:
    script = Path("packaging/install.ps1").read_text(encoding="utf-8")
    assert "EnableLaunchAtLogin = $false" in script

def test_uninstaller_preserves_user_data_unless_requested() -> None:
    script = Path("packaging/uninstall.ps1").read_text(encoding="utf-8")
    assert "-RemoveUserData" in script
    assert "if ($RemoveUserData)" in script

def test_pyinstaller_is_one_folder() -> None:
    spec = Path("packaging/harbor-voice.spec").read_text(encoding="utf-8")
    assert "COLLECT(" in spec
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_packaging.py -v`

Expected: packaging files do not exist.

- [ ] **Step 3: Implement packaging and documentation**

Document prerequisites, privacy, permissions, first run, model download size, shortcut use, diagnostics, settings location, and uninstallation. Configure PyInstaller to collect PySide6, faster-whisper/CTranslate2, sounddevice, pyttsx3, and openai-codex metadata without bundling credentials or model caches. Install to `%LOCALAPPDATA%\Programs\HarborVoice`; create only a Start Menu shortcut by default. `-EnableLaunchAtLogin` is an explicit installer switch. `-RemoveUserData` is an explicit uninstaller switch.

```powershell
param(
    [switch]$EnableLaunchAtLogin = $false
)
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\HarborVoice'
```

- [ ] **Step 4: Run full automated verification**

Run:

```powershell
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q src
uv run pyinstaller --clean --noconfirm packaging/harbor-voice.spec
dist\HarborVoice\harbor-voice-doctor.exe --json
```

Expected: lint and tests exit zero, compilation succeeds, PyInstaller produces `dist\HarborVoice`, and the packaged diagnostic returns valid JSON. Hardware or authentication checks may report `unavailable`, but the diagnostic process must exit normally.

- [ ] **Step 5: Perform bounded Windows smoke checks**

Run the packaged app and verify one instance, tray visibility, show/hide, settings, shortcut registration, microphone capture, local transcript, SAPI playback, speech interruption, Codex sign-in detection, read-only conversation, rejection with zero effect, and one approved HTTPS launch. Do not test file deletion or unrestricted shell execution because those features must not exist.

- [ ] **Step 6: Commit**

```powershell
git add README.md packaging tests/test_packaging.py
git commit -m "build: package and document Harbor Voice for Windows"
```

---

## Final verification checklist

- [ ] `git diff --check` reports no whitespace errors.
- [ ] `uv lock --check` confirms the lockfile is current.
- [ ] `uv run ruff check .` reports zero findings.
- [ ] `uv run pytest -q` reports zero failures and zero warnings.
- [ ] `uv run python -m compileall -q src` exits zero.
- [ ] The PyInstaller build exits zero and the packaged doctor emits valid JSON.
- [ ] The acceptance criteria in the design specification are checked one by one against tests or a recorded smoke result.
- [ ] `git status --short` contains no unintended build artifacts, caches, credentials, model files, recordings, or user data.
- [ ] Dependency and model licenses are listed before any public distribution.
