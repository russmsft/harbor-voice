"""Loopback WebSocket bridge between Harbor Voice and the headless ACS participant page."""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import struct
import subprocess
import tempfile
import threading
import winreg
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

STATIC_DIR = Path(__file__).with_name("static")
SAMPLE_RATE = 48_000
_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8"}

StatusCallback = Callable[[str, str], None]
AudioCallback = Callable[[bytes, int, int], None]


class BridgeUnavailable(RuntimeError):
    """The participant page could not be started or connected."""


def find_edge() -> str:
    candidates: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        with suppress(OSError):
            key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
            with winreg.OpenKey(hive, key) as handle:
                candidates.append(winreg.QueryValue(handle, None))
    candidates += [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise BridgeUnavailable("Microsoft Edge was not found")


class EdgeLauncher:
    """Runs the participant page in an isolated, throwaway Edge profile."""

    def __init__(self, *, headless: bool = True, executable: str | None = None) -> None:
        self._headless = headless
        self._executable = executable
        self._process: subprocess.Popen[bytes] | None = None
        self._profile: Path | None = None

    def launch(self, url: str) -> None:
        executable = self._executable or find_edge()
        self._profile = Path(tempfile.mkdtemp(prefix="harbor-acs-"))
        arguments = [
            executable,
            f"--user-data-dir={self._profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
        ]
        if self._headless:
            arguments += ["--headless=new", url]
        else:
            arguments.append(f"--app={url}")
        self._process = subprocess.Popen(
            arguments,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(5)
            if process.poll() is None:
                process.kill()
        profile, self._profile = self._profile, None
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)


class AcsMeetingTransport:
    """Meeting transport whose far end is the headless ACS participant page.

    It implements the same ``play``/``clear`` contract as the local audio bridge, so the
    meeting agent and speaker work unchanged.
    """

    def __init__(
        self,
        *,
        meeting_link: str,
        display_name: str,
        token_provider: Any,
        on_participant_audio: AudioCallback,
        on_status: StatusCallback,
        launcher: Any = None,
        connect_timeout: float = 45.0,
    ) -> None:
        self._meeting_link = meeting_link
        self._display_name = display_name
        self._token_provider = token_provider
        self._on_audio = on_participant_audio
        self._on_status = on_status
        self._launcher = launcher or EdgeLauncher()
        self._connect_timeout = connect_timeout
        self._nonce = secrets.token_urlsafe(24)
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._server_thread: threading.Thread | None = None
        self._server = None
        self._connection = None
        self._connected = threading.Event()
        self._port = 0
        self._next_id = 0
        self._pending: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = {}
        self._pending_lock = threading.Lock()
        self.device_label = f"the Teams meeting as {display_name}"
        self.state = "starting"

    async def start(self) -> None:
        ready: Future[None] = Future()
        self._server_thread = threading.Thread(
            target=self._run_server, args=(ready,), name="harbor-acs-bridge", daemon=True
        )
        self._server_thread.start()
        await asyncio.wrap_future(ready)
        try:
            self._launcher.launch(f"http://localhost:{self._port}/?nonce={self._nonce}")
            connected = await asyncio.to_thread(self._connected.wait, self._connect_timeout)
            if not connected:
                raise BridgeUnavailable("the meeting participant page did not start")
            token = await self._token_provider.issue()
            self._send_text(
                {
                    "type": "join",
                    "token": token,
                    "meetingLink": self._meeting_link,
                    "displayName": self._display_name,
                }
            )
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._connection is not None:
            with suppress(Exception):
                self._send_text({"type": "leave"})
                await asyncio.sleep(1.0)
        self._resolve_all()
        loop, self._server_loop = self._server_loop, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        thread, self._server_thread = self._server_thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, 5)
        await asyncio.to_thread(self._launcher.close)
        self._connection = None

    async def play(self, pcm: bytes) -> None:
        if not pcm or self._connection is None:
            return
        loop = asyncio.get_running_loop()
        finished: asyncio.Future[None] = loop.create_future()
        with self._pending_lock:
            self._next_id = (self._next_id + 1) % 0xFFFFFFFF
            identifier = self._next_id
            self._pending[identifier] = (loop, finished)
        self._send_binary(struct.pack("<I", identifier) + pcm)
        await finished

    def clear(self) -> None:
        with suppress(Exception):
            self._send_text({"type": "clear"})
        self._resolve_all()

    # --- server thread -------------------------------------------------------------------

    def _run_server(self, ready: Future[None]) -> None:
        loop = asyncio.new_event_loop()
        self._server_loop = loop
        try:
            from websockets.asyncio.server import serve

            async def open_server():
                return await serve(
                    self._handle,
                    "127.0.0.1",
                    0,
                    process_request=self._process_request,
                    max_size=2**22,
                )

            self._server = loop.run_until_complete(open_server())
            self._port = self._server.sockets[0].getsockname()[1]
        except BaseException as exc:
            ready.set_exception(exc)
            loop.close()
            return
        ready.set_result(None)
        try:
            loop.run_forever()
        finally:
            self._server.close()
            with suppress(Exception):
                loop.run_until_complete(self._server.wait_closed())
            loop.close()

    def _process_request(self, connection, request):
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        parts = urlsplit(request.path)
        if parts.path == "/bridge":
            if parse_qs(parts.query).get("nonce") != [self._nonce] or self._connection:
                return connection.respond(403, "Forbidden\n")
            return None
        name = {"/": "index.html", "/bridge.js": "bridge.js"}.get(parts.path)
        if name is None or (
            parts.path == "/" and parse_qs(parts.query).get("nonce") != [self._nonce]
        ):
            return connection.respond(404, "Not found\n")
        path = STATIC_DIR / name
        if not path.is_file():
            return connection.respond(503, "Participant page is not built\n")
        body = path.read_bytes()
        headers = Headers(
            [
                ("Content-Type", _CONTENT_TYPES[path.suffix]),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ]
        )
        return Response(200, "OK", headers, body)

    async def _handle(self, connection) -> None:
        self._connection = connection
        self._connected.set()
        try:
            async for message in connection:
                if isinstance(message, bytes):
                    self._on_audio(message, SAMPLE_RATE, 1)
                else:
                    self._on_text(json.loads(message))
        finally:
            self._connection = None
            self._resolve_all()
            self._emit_status("closed", "")

    def _on_text(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "played":
            self._resolve(int(message.get("id", -1)))
        elif kind == "status":
            self._emit_status(str(message.get("state", "")), str(message.get("detail", "")))

    def _emit_status(self, state: str, detail: str) -> None:
        self.state = state
        self._on_status(state, detail)

    def _send_text(self, payload: dict[str, Any]) -> None:
        self._send(json.dumps(payload))

    def _send_binary(self, payload: bytes) -> None:
        self._send(payload)

    def _send(self, payload: str | bytes) -> None:
        loop, connection = self._server_loop, self._connection
        if loop is None or connection is None:
            return
        asyncio.run_coroutine_threadsafe(connection.send(payload), loop)

    def _resolve(self, identifier: int) -> None:
        with self._pending_lock:
            entry = self._pending.pop(identifier, None)
        if entry is not None:
            loop, future = entry
            loop.call_soon_threadsafe(_finish, future)

    def _resolve_all(self) -> None:
        with self._pending_lock:
            entries = list(self._pending.values())
            self._pending.clear()
        for loop, future in entries:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_finish, future)


def _finish(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)
