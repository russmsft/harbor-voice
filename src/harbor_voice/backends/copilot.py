"""Structured GitHub Copilot CLI reasoning with a read-only tool boundary."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from harbor_voice.domain import (
    ActionKind,
    ActionProposal,
    ActionResult,
    AssistantRequest,
    AssistantResponse,
    MessageResponse,
    parse_assistant_response,
)

VOICE_INSTRUCTIONS = """You are Harbor Voice's GitHub Copilot reasoning provider.
Reply concisely in natural spoken language. Return only one raw JSON object matching
the supplied JSON schema, without Markdown fences or any surrounding explanation.
Return a message for information and read-only work. Return a proposal when the user
explicitly asks to write a file, open an application, open an HTTPS URL, read the
clipboard, or replace clipboard text. Never claim an action has happened when you
are only proposing it. Never propose deletion, credentials, arbitrary shell access,
or work outside the current folder. A file_write proposal must include the complete
final UTF-8 file text in action.payload.content. The application, not you, performs
that exact single-file write after approval.
"""

ASSISTANT_RESPONSE_SCHEMA = TypeAdapter(AssistantResponse).json_schema()
_SAFE_PARSE_FAILURE = "I couldn't safely interpret that response, so I took no action."
_MAX_HISTORY_ITEMS = 12
_MAX_HISTORY_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _BasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("security_descriptor", ctypes.c_void_p),
        ("inherit_handle", wintypes.BOOL),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD),
        ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD),
        ("y_size", wintypes.DWORD),
        ("x_count_chars", wintypes.DWORD),
        ("y_count_chars", wintypes.DWORD),
        ("fill_attribute", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("show_window", wintypes.WORD),
        ("reserved_size", wintypes.WORD),
        ("reserved_data", ctypes.POINTER(wintypes.BYTE)),
        ("stdin", wintypes.HANDLE),
        ("stdout", wintypes.HANDLE),
        ("stderr", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE),
        ("thread", wintypes.HANDLE),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [
        ("startup_info", _StartupInfo),
        ("attribute_list", ctypes.c_void_p),
    ]


class _WindowsJob:
    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, kernel32=None) -> None:
        kernel32 = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = self._KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            self._handle,
            self._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not configured:
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process: wintypes.HANDLE) -> None:
        if not self._kernel32.AssignProcessToJobObject(self._handle, process):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._kernel32.CloseHandle(handle)


class _WindowsProcess:
    _CREATE_NO_WINDOW = 0x08000000
    _CREATE_SUSPENDED = 0x00000004
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _HANDLE_FLAG_INHERIT = 0x00000001
    _HANDLE_LIST_ATTRIBUTE = 0x00020002
    _STARTF_USESTDHANDLES = 0x00000100
    _INFINITE = 0xFFFFFFFF
    _WAIT_OBJECT_0 = 0

    def __init__(
        self,
        arguments: list[str],
        cwd: Path,
        environment: dict[str, str],
    ) -> None:
        import msvcrt

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_functions()
        self._job = _WindowsJob(self._kernel32)
        self._process: wintypes.HANDLE | None = None
        self._stdin = None
        self._stdout = None
        self._stderr = None
        security = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            None,
            True,
        )
        stdin_read, stdin_write = wintypes.HANDLE(), wintypes.HANDLE()
        stdout_read, stdout_write = wintypes.HANDLE(), wintypes.HANDLE()
        stderr_read, stderr_write = wintypes.HANDLE(), wintypes.HANDLE()
        process_info = _ProcessInformation()
        thread = None
        attribute_list = None
        try:
            self._create_input_pipe(stdin_read, stdin_write, security)
            self._create_pipe(stdout_read, stdout_write, security)
            self._create_pipe(stderr_read, stderr_write, security)
            startup = _StartupInfoEx()
            startup.startup_info.cb = ctypes.sizeof(_StartupInfoEx)
            startup.startup_info.flags = self._STARTF_USESTDHANDLES
            startup.startup_info.stdin = stdin_read
            startup.startup_info.stdout = stdout_write
            startup.startup_info.stderr = stderr_write
            attribute_list, handle_list = self._create_handle_list(
                stdin_read,
                stdout_write,
                stderr_write,
            )
            startup.attribute_list = ctypes.cast(attribute_list, ctypes.c_void_p)
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
            environment_block = ctypes.create_unicode_buffer(
                "\0".join(
                    f"{name}={value}"
                    for name, value in sorted(environment.items(), key=lambda item: item[0].upper())
                )
                + "\0\0"
            )
            created = self._kernel32.CreateProcessW(
                arguments[0],
                command_line,
                None,
                None,
                True,
                self._CREATE_NO_WINDOW
                | self._CREATE_SUSPENDED
                | self._CREATE_UNICODE_ENVIRONMENT
                | self._EXTENDED_STARTUPINFO_PRESENT,
                environment_block,
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            )
            if not created:
                raise ctypes.WinError(ctypes.get_last_error())
            self._process = process_info.process
            thread = process_info.thread
            self._job.assign(process_info.process)
            if self._kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            self._kernel32.CloseHandle(thread)
            thread = None
            self._close_handle(stdin_read)
            stdin_read = wintypes.HANDLE()
            self._close_handle(stdout_write)
            stdout_write = wintypes.HANDLE()
            self._close_handle(stderr_write)
            stderr_write = wintypes.HANDLE()
            stdin_fd = msvcrt.open_osfhandle(stdin_write.value, os.O_WRONLY | os.O_BINARY)
            stdin_write = wintypes.HANDLE()
            self._stdin = os.fdopen(stdin_fd, "wb", buffering=0)
            stdout_fd = msvcrt.open_osfhandle(stdout_read.value, os.O_RDONLY | os.O_BINARY)
            stdout_read = wintypes.HANDLE()
            self._stdout = os.fdopen(stdout_fd, "rb", buffering=0)
            stderr_fd = msvcrt.open_osfhandle(stderr_read.value, os.O_RDONLY | os.O_BINARY)
            stderr_read = wintypes.HANDLE()
            self._stderr = os.fdopen(stderr_fd, "rb", buffering=0)
        except Exception:
            self._job.close()
            if self._process:
                self._kernel32.TerminateProcess(self._process, 1)
                self._kernel32.WaitForSingleObject(self._process, 5_000)
                self._kernel32.CloseHandle(self._process)
                self._process = None
            if self._stdin is not None:
                self._stdin.close()
                self._stdin = None
            if self._stdout is not None:
                self._stdout.close()
                self._stdout = None
            if self._stderr is not None:
                self._stderr.close()
                self._stderr = None
            raise
        finally:
            if attribute_list is not None:
                self._kernel32.DeleteProcThreadAttributeList(
                    ctypes.cast(attribute_list, ctypes.c_void_p)
                )
            for handle in (
                thread,
                stdin_read,
                stdin_write,
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
            ):
                self._close_handle(handle)

    async def communicate(self) -> CliResult:
        await self.close_stdin()
        stdout_task = asyncio.create_task(asyncio.to_thread(self._stdout.read))
        stderr_task = asyncio.create_task(asyncio.to_thread(self._stderr.read))
        wait_task = asyncio.create_task(asyncio.to_thread(self._wait))
        read_future = asyncio.gather(stdout_task, stderr_task)
        try:
            returncode = await asyncio.shield(wait_task)
            stdout, stderr = await asyncio.shield(read_future)
        except asyncio.CancelledError:
            self._job.close()
            await asyncio.shield(wait_task)
            await asyncio.shield(read_future)
            raise
        except Exception:
            self._job.close()
            await asyncio.gather(wait_task, read_future, return_exceptions=True)
            raise
        finally:
            self.close()
        return CliResult(
            returncode=returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    async def write_line(self, line: str) -> None:
        if self._stdin is None:
            raise RuntimeError("Copilot ACP stdin is closed")
        await asyncio.to_thread(self._write_line, line)

    def _write_line(self, line: str) -> None:
        self._stdin.write(line.encode("utf-8") + b"\n")
        self._stdin.flush()

    async def read_stdout_line(self) -> str:
        if self._stdout is None:
            return ""
        line = await asyncio.to_thread(self._stdout.readline)
        return line.decode("utf-8", errors="replace")

    async def read_stderr(self) -> str:
        if self._stderr is None:
            return ""
        content = await asyncio.to_thread(self._stderr.read)
        return content.decode("utf-8", errors="replace")

    async def close_stdin(self) -> None:
        stdin, self._stdin = self._stdin, None
        if stdin is not None:
            await asyncio.to_thread(stdin.close)

    async def wait(self) -> int:
        return await asyncio.to_thread(self._wait)

    def terminate(self) -> None:
        self._job.close()

    def close(self) -> None:
        self._job.close()
        if self._stdin is not None:
            self._stdin.close()
            self._stdin = None
        if self._stdout is not None:
            self._stdout.close()
            self._stdout = None
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        process, self._process = self._process, None
        self._close_handle(process)

    def _wait(self) -> int:
        result = self._kernel32.WaitForSingleObject(self._process, self._INFINITE)
        if result != self._WAIT_OBJECT_0:
            raise ctypes.WinError(ctypes.get_last_error())
        exit_code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(self._process, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value

    def _create_pipe(
        self,
        read: wintypes.HANDLE,
        write: wintypes.HANDLE,
        security: _SecurityAttributes,
    ) -> None:
        if not self._kernel32.CreatePipe(
            ctypes.byref(read),
            ctypes.byref(write),
            ctypes.byref(security),
            0,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self._kernel32.SetHandleInformation(
            read,
            self._HANDLE_FLAG_INHERIT,
            0,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def _create_input_pipe(
        self,
        read: wintypes.HANDLE,
        write: wintypes.HANDLE,
        security: _SecurityAttributes,
    ) -> None:
        if not self._kernel32.CreatePipe(
            ctypes.byref(read),
            ctypes.byref(write),
            ctypes.byref(security),
            0,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self._kernel32.SetHandleInformation(
            write,
            self._HANDLE_FLAG_INHERIT,
            0,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def _create_handle_list(
        self,
        stdin: wintypes.HANDLE,
        stdout: wintypes.HANDLE,
        stderr: wintypes.HANDLE,
    ):
        size = ctypes.c_size_t()
        self._kernel32.InitializeProcThreadAttributeList(
            None,
            1,
            0,
            ctypes.byref(size),
        )
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        attribute_list = ctypes.create_string_buffer(size.value)
        pointer = ctypes.cast(attribute_list, ctypes.c_void_p)
        if not self._kernel32.InitializeProcThreadAttributeList(
            pointer,
            1,
            0,
            ctypes.byref(size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        handles = (wintypes.HANDLE * 3)(stdin, stdout, stderr)
        if not self._kernel32.UpdateProcThreadAttribute(
            pointer,
            0,
            self._HANDLE_LIST_ATTRIBUTE,
            ctypes.cast(handles, ctypes.c_void_p),
            ctypes.sizeof(handles),
            None,
            None,
        ):
            self._kernel32.DeleteProcThreadAttributeList(pointer)
            raise ctypes.WinError(ctypes.get_last_error())
        return attribute_list, handles

    def _configure_functions(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(_SecurityAttributes),
            wintypes.DWORD,
        ]
        kernel32.CreatePipe.restype = wintypes.BOOL
        kernel32.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.SetHandleInformation.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessInformation),
        ]
        kernel32.CreateProcessW.restype = wintypes.BOOL
        kernel32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel32.DeleteProcThreadAttributeList.restype = None
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def _close_handle(self, handle) -> None:
        if handle:
            self._kernel32.CloseHandle(handle)


async def _run_cli(
    arguments: list[str],
    cwd: Path,
    environment: dict[str, str],
) -> CliResult:
    if os.name == "nt":
        return await _WindowsProcess(arguments, cwd, environment).communicate()
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=str(cwd),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return CliResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


class AcpProcess(Protocol):
    async def write_line(self, line: str) -> None: ...

    async def read_stdout_line(self) -> str: ...

    async def read_stderr(self) -> str: ...

    async def close_stdin(self) -> None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


class _AsyncioAcpProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def write_line(self, line: str) -> None:
        if self._process.stdin is None:
            raise RuntimeError("Copilot ACP stdin is closed")
        self._process.stdin.write(line.encode("utf-8") + b"\n")
        await self._process.stdin.drain()

    async def read_stdout_line(self) -> str:
        if self._process.stdout is None:
            return ""
        line = await self._process.stdout.readline()
        return line.decode("utf-8", errors="replace")

    async def read_stderr(self) -> str:
        if self._process.stderr is None:
            return ""
        content = await self._process.stderr.read()
        return content.decode("utf-8", errors="replace")

    async def close_stdin(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
            await self._process.stdin.wait_closed()

    async def wait(self) -> int:
        return await self._process.wait()

    def terminate(self) -> None:
        if self._process.returncode is None:
            self._process.kill()

    def close(self) -> None:
        return None


async def _start_acp_process(
    arguments: list[str],
    cwd: Path,
    environment: dict[str, str],
) -> AcpProcess:
    if os.name == "nt":
        return _WindowsProcess(arguments, cwd, environment)
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=str(cwd),
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return _AsyncioAcpProcess(process)


AcpProcessFactory = Callable[[list[str], Path, dict[str, str]], Awaitable[AcpProcess]]


class CopilotAcpConnection:
    def __init__(
        self,
        arguments: list[str],
        cwd: Path,
        environment: dict[str, str],
        *,
        process_factory: AcpProcessFactory = _start_acp_process,
    ) -> None:
        self.arguments = list(arguments)
        self.cwd = cwd
        self.environment = dict(environment)
        self._process_factory = process_factory
        self._process: AcpProcess | None = None
        self._stderr_task: asyncio.Task[str] | None = None
        self._request_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._next_request_id = 0

    async def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("Copilot ACP connection is already started")
        self._process = await self._process_factory(
            self.arguments,
            self.cwd,
            self.environment,
        )
        self._stderr_task = asyncio.create_task(self._process.read_stderr())
        try:
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "harbor-voice", "version": "0.1.0"},
                },
            )
        except BaseException:
            await self.close()
            raise
        if result.get("protocolVersion") != 1:
            await self.close()
            raise RuntimeError("GitHub Copilot CLI does not support ACP protocol version 1")

    async def new_session(self, workspace: Path) -> str:
        result = await self._request(
            "session/new",
            {"cwd": str(workspace), "mcpServers": []},
        )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("GitHub Copilot ACP did not return a session identifier")
        return session_id

    async def prompt(self, session_id: str, text: str) -> str:
        messages: dict[str, list[str]] = {}
        message_order: list[str] = []
        anonymous_message = 0
        active_anonymous_id: str | None = None

        def collect(message: dict[str, Any]) -> None:
            nonlocal active_anonymous_id, anonymous_message
            if message.get("method") != "session/update":
                return
            params = message.get("params")
            if not isinstance(params, dict):
                return
            update = params.get("update")
            if not isinstance(update, dict):
                return
            update_type = update.get("sessionUpdate")
            if update_type != "agent_message_chunk":
                if update_type in {
                    "agent_thought_chunk",
                    "plan",
                    "tool_call",
                    "tool_call_update",
                }:
                    active_anonymous_id = None
                return
            content = update.get("content")
            if not isinstance(content, dict):
                return
            chunk = content.get("text")
            if isinstance(chunk, str):
                message_id = update.get("messageId")
                if not isinstance(message_id, str) or not message_id:
                    if active_anonymous_id is None:
                        anonymous_message += 1
                        active_anonymous_id = f"__anonymous_{anonymous_message}"
                    message_id = active_anonymous_id
                if message_id not in messages:
                    messages[message_id] = []
                    message_order.append(message_id)
                messages[message_id].append(chunk)

        result = await self._request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
            notification_handler=collect,
        )
        stop_reason = result.get("stopReason")
        if stop_reason not in {"end_turn", "cancelled"}:
            raise RuntimeError(f"GitHub Copilot ACP stopped unexpectedly: {stop_reason}")
        if not message_order:
            return ""
        return "".join(messages[message_order[-1]])

    async def cancel(self, session_id: str) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session_id},
            }
        )

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            await process.close_stdin()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.terminate()
                await process.wait()
        finally:
            process.close()
            stderr_task, self._stderr_task = self._stderr_task, None
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)

    async def abort(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        finally:
            process.close()
            stderr_task, self._stderr_task = self._stderr_task, None
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        notification_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        async with self._request_lock:
            self._next_request_id += 1
            request_id = self._next_request_id
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            while True:
                message = await self._read_message()
                if message.get("id") == request_id and "method" not in message:
                    if "error" in message:
                        raise RuntimeError(
                            f"GitHub Copilot ACP request failed: {message['error']}"
                        )
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise RuntimeError("GitHub Copilot ACP returned an invalid result")
                    return result
                if "id" in message and "method" in message:
                    await self._deny_server_request(message)
                elif notification_handler is not None:
                    notification_handler(message)

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        async with self._write_lock:
            await process.write_line(encoded)

    async def _read_message(self) -> dict[str, Any]:
        line = await self._require_process().read_stdout_line()
        if not line:
            detail = ""
            if self._stderr_task is not None and self._stderr_task.done():
                detail = self._stderr_task.result().strip()
            suffix = f": {detail[:500]}" if detail else ""
            raise RuntimeError(f"GitHub Copilot ACP closed unexpectedly{suffix}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub Copilot ACP returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise RuntimeError("GitHub Copilot ACP returned an invalid message")
        return message

    async def _deny_server_request(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        if message.get("method") == "session/request_permission":
            params = message.get("params")
            options = params.get("options", []) if isinstance(params, dict) else []
            reject_option = next(
                (
                    option
                    for option in options
                    if isinstance(option, dict)
                    and option.get("kind") in {"reject_once", "reject_always"}
                    and isinstance(option.get("optionId"), str)
                ),
                None,
            )
            if reject_option is None:
                outcome = {"outcome": "cancelled"}
            else:
                outcome = {
                    "outcome": "selected",
                    "optionId": reject_option["optionId"],
                }
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"outcome": outcome},
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Client method is not supported"},
            }
        await self._send(response)

    def _require_process(self) -> AcpProcess:
        if self._process is None:
            raise RuntimeError("GitHub Copilot ACP connection is not started")
        return self._process


class AcpConnection(Protocol):
    async def start(self) -> None: ...

    async def new_session(self, workspace: Path) -> str: ...

    async def prompt(self, session_id: str, text: str) -> str: ...

    async def cancel(self, session_id: str) -> None: ...

    async def close(self) -> None: ...

    async def abort(self) -> None: ...


AcpConnectionFactory = Callable[[list[str], Path, dict[str, str]], AcpConnection]


class CopilotCliBackend:
    def __init__(
        self,
        *,
        copilot_home: Path,
        executable: str | None = None,
        connection_factory: AcpConnectionFactory = CopilotAcpConnection,
        timeout_seconds: float = 180.0,
        startup_timeout_seconds: float = 30.0,
    ) -> None:
        self._configured_executable = executable
        self._copilot_home = copilot_home.expanduser().resolve(strict=False)
        self._executable: str | None = None
        self._connection_factory = connection_factory
        self._timeout_seconds = timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._connection: AcpConnection | None = None
        self._session_id: str | None = None
        self._primed = False
        self._history: list[tuple[str, str]] = []
        self.workspace: Path | None = None

    async def start(self, workspace: Path) -> None:
        resolved = workspace.expanduser().resolve(strict=False)
        if not resolved.is_dir():
            raise ValueError("workspace must exist and be a directory")
        if self.workspace is not None:
            raise RuntimeError("GitHub Copilot CLI backend is already started")
        executable = self._configured_executable or shutil.which("copilot")
        if executable is None:
            raise RuntimeError(
                "GitHub Copilot CLI was not found. Install it and run 'copilot login'."
            )
        self._executable = executable
        self._prepare_copilot_home()
        self.workspace = resolved
        try:
            await self._open_connection()
        except BaseException:
            self.workspace = None
            self._executable = None
            raise
        self._history.clear()
        self._primed = False

    async def _open_connection(self) -> None:
        workspace = self._require_workspace()
        connection = self._connection_factory(
            self._server_arguments(),
            workspace,
            self._environment(),
        )
        self._connection = connection
        try:
            await asyncio.wait_for(
                connection.start(),
                timeout=self._startup_timeout_seconds,
            )
            self._session_id = await asyncio.wait_for(
                connection.new_session(workspace),
                timeout=self._startup_timeout_seconds,
            )
        except BaseException:
            await connection.abort()
            self._connection = None
            self._session_id = None
            raise

    async def ask(self, request: AssistantRequest) -> AssistantResponse:
        raw = ""
        for attempt in range(2):
            await self._ensure_connection()
            prompt = self._build_prompt(request.text)
            connection = self._require_connection()
            session_id = self._require_session_id()
            prompt_task = asyncio.create_task(connection.prompt(session_id, prompt))
            try:
                raw = await asyncio.wait_for(
                    asyncio.shield(prompt_task),
                    timeout=self._timeout_seconds,
                )
                break
            except TimeoutError as exc:
                await self._cancel_and_drain(connection, session_id, prompt_task)
                raise RuntimeError("GitHub Copilot CLI timed out") from exc
            except asyncio.CancelledError:
                await self._cancel_and_drain(connection, session_id, prompt_task)
                raise
            except (OSError, RuntimeError):
                await self._disconnect_connection(connection, prompt_task)
                if attempt > 0:
                    raise
        self._primed = True
        raw = _extract_response_json(raw)
        try:
            response = parse_assistant_response(raw)
        except ValidationError:
            response = MessageResponse(kind="message", message=_SAFE_PARSE_FAILURE)
        self._history.extend(
            [
                ("user", request.text),
                ("assistant", response.model_dump_json()),
            ]
        )
        self._trim_history()
        return response

    async def apply_workspace_change(self, action: ActionProposal) -> ActionResult:
        if action.kind is not ActionKind.FILE_WRITE:
            raise ValueError("workspace-write is restricted to an approved file-write action")
        content = action.payload.get("content")
        if content is None:
            raise ValueError("workspace-write requires complete file content")
        workspace = self._require_workspace()
        candidate = Path(action.target).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        target = candidate.resolve(strict=False)
        if target != workspace and not target.is_relative_to(workspace):
            raise PermissionError("file target is outside workspace")
        if target.is_dir():
            raise PermissionError("file target is a directory")
        if not target.parent.is_dir():
            raise PermissionError("file target parent directory does not exist")

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return ActionResult(
            success=True,
            message=f"Updated {target.name}.",
            details={"target": str(target)},
        )

    async def reset(self) -> None:
        workspace = self._require_workspace()
        self._history.clear()
        self._primed = False
        connection = self._connection
        if connection is None:
            await self._ensure_connection()
            return
        self._session_id = None
        try:
            self._session_id = await asyncio.wait_for(
                connection.new_session(workspace),
                timeout=self._startup_timeout_seconds,
            )
        except (TimeoutError, OSError, RuntimeError):
            await self._restart_connection(connection)

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        self._session_id = None
        self._primed = False
        self._history.clear()
        self.workspace = None
        self._executable = None
        if connection is not None:
            await connection.close()

    def _require_workspace(self) -> Path:
        if self.workspace is None:
            raise RuntimeError("GitHub Copilot CLI backend is not started")
        return self.workspace

    def _server_arguments(self) -> list[str]:
        executable = self._executable
        if executable is None:
            raise RuntimeError("GitHub Copilot CLI backend is not started")
        return [
            executable,
            "--acp",
            "--stdio",
            "--no-color",
            "--no-ask-user",
            "--no-auto-update",
            "--no-custom-instructions",
            "--no-remote",
            "--no-remote-export",
            "--disable-builtin-mcps",
            "--disallow-temp-dir",
            "--effort",
            "low",
            "--log-level",
            "none",
            "--available-tools",
            "view,grep,glob",
            "--allow-tool",
            "view,grep,glob",
        ]

    def _build_prompt(self, request: str) -> str:
        if self._primed:
            return (
                "Continue following the Harbor Voice JSON response contract. "
                f"Current user request:\n{request}"
            )
        schema = json.dumps(ASSISTANT_RESPONSE_SCHEMA, separators=(",", ":"))
        history = json.dumps(
            [{"role": role, "content": text} for role, text in self._history],
            separators=(",", ":"),
        )
        return (
            f"{VOICE_INSTRUCTIONS}\nJSON schema:\n{schema}\n"
            f"Prior conversation JSON:\n{history}\n"
            f"Current user request:\n{request}"
        )

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        blocked_names = {
            "COPILOT_GITHUB_TOKEN",
            "COPILOT_GH_HOST",
            "GH_HOST",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        }
        for name in tuple(environment):
            if name in blocked_names or (
                name.startswith("COPILOT_PROVIDER_")
                or name.startswith("COPILOT_OTEL_")
                or name.startswith("OTEL_")
                or name.startswith("GITHUB_COPILOT_PROMPT_MODE_")
            ):
                environment.pop(name)
        environment.update(
            {
                "COPILOT_HOME": str(self._copilot_home),
                "COPILOT_ALLOW_ALL": "false",
                "COPILOT_AUTO_UPDATE": "false",
                "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "",
                "COPILOT_OTEL_ENABLED": "false",
                "NO_COLOR": "1",
            }
        )
        return environment

    def _prepare_copilot_home(self) -> None:
        self._copilot_home.mkdir(parents=True, exist_ok=True)
        safe_settings = {
            "autoUpdate": False,
            "disableAllHooks": True,
            "hooks": {},
            "ide": {"autoConnect": False},
            "memory": False,
            "trustedFolders": [],
        }
        content = json.dumps(safe_settings, indent=2)
        for name in ("config.json", "settings.json"):
            path = self._copilot_home / name
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temporary, path)

    def _require_connection(self) -> AcpConnection:
        if self._connection is None:
            raise RuntimeError("GitHub Copilot CLI backend is not started")
        return self._connection

    async def _ensure_connection(self) -> None:
        if self._connection is None:
            await self._open_connection()
            self._primed = False

    def _require_session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("GitHub Copilot CLI backend is not started")
        return self._session_id

    def _trim_history(self) -> None:
        self._history = self._history[-_MAX_HISTORY_ITEMS:]
        while sum(len(text) for _, text in self._history) > _MAX_HISTORY_CHARS:
            self._history.pop(0)

    async def _cancel_and_drain(
        self,
        connection: AcpConnection,
        session_id: str,
        prompt_task: asyncio.Task[str],
    ) -> None:
        clean_cancel = True
        try:
            await asyncio.wait_for(connection.cancel(session_id), timeout=2)
        except (TimeoutError, OSError, RuntimeError):
            clean_cancel = False
        if clean_cancel:
            try:
                await asyncio.wait_for(asyncio.shield(prompt_task), timeout=10)
                return
            except (TimeoutError, OSError, RuntimeError):
                pass

        self._connection = None
        self._session_id = None
        await self._disconnect_connection(connection, prompt_task)

    async def _disconnect_connection(
        self,
        connection: AcpConnection,
        prompt_task: asyncio.Task[str],
    ) -> None:
        self._connection = None
        self._session_id = None
        await connection.abort()
        prompt_task.cancel()
        await asyncio.gather(prompt_task, return_exceptions=True)
        self._primed = False

    async def _restart_connection(self, connection: AcpConnection) -> None:
        self._connection = None
        self._session_id = None
        await connection.abort()
        await self._open_connection()
        self._primed = False


def _extract_response_json(raw: str) -> str:
    text = raw.strip()
    for line in reversed(text.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "assistant.message":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        content = data.get("content")
        if isinstance(content, str):
            text = content.strip()
            break
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    decoder = json.JSONDecoder()
    for index in range(len(text) - 1, -1, -1):
        if text[index] != "{":
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("kind") == "message"
            and not text[end:].strip()
        ):
            return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return text
