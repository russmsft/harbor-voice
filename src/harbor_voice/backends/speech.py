"""Cancellable Windows SAPI speech on a dedicated worker."""

from __future__ import annotations

import asyncio
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def split_for_speech(text: str, max_chars: int = 320) -> list[str]:
    """Split prose into bounded segments without changing word order."""
    sentences = [part.strip() for part in _SENTENCE_BOUNDARY.split(text.strip()) if part.strip()]
    segments: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            segments.append(sentence)
            continue
        current: list[str] = []
        current_length = 0
        for word in sentence.split():
            projected = current_length + len(word) + (1 if current else 0)
            if current and projected > max_chars:
                segments.append(" ".join(current))
                current = [word]
                current_length = len(word)
            else:
                current.append(word)
                current_length = projected
        if current:
            segments.append(" ".join(current))
    return segments


def _default_engine_factory():
    import pyttsx3

    return pyttsx3.init()


@dataclass(slots=True)
class _SpeechCommand:
    generation: int
    text: str
    future: asyncio.Future[None]
    loop: asyncio.AbstractEventLoop


class SapiSpeaker:
    def __init__(self, *, engine_factory: Callable[[], Any] = _default_engine_factory) -> None:
        self._engine_factory = engine_factory
        self._commands: queue.Queue[_SpeechCommand | None] = queue.Queue()
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._engine = None
        self._startup_error: Exception | None = None
        self._startup_ready = threading.Event()
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="harbor-sapi", daemon=True)
        self._worker.start()

    async def speak(self, text: str) -> None:
        await asyncio.to_thread(self._startup_ready.wait)
        if self._startup_error is not None:
            raise RuntimeError("SAPI speech engine failed to initialize") from self._startup_error
        generation = self._current_generation()
        for segment in split_for_speech(text):
            if generation != self._current_generation() or self._closed:
                return
            loop = asyncio.get_running_loop()
            future: asyncio.Future[None] = loop.create_future()
            self._commands.put(_SpeechCommand(generation, segment, future, loop))
            await future

    def cancel(self) -> None:
        with self._generation_lock:
            self._generation += 1
        engine = self._engine
        if engine is not None:
            engine.stop()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel()
        self._commands.put(None)
        await asyncio.to_thread(self._worker.join, 2)

    def _current_generation(self) -> int:
        with self._generation_lock:
            return self._generation

    def _run(self) -> None:
        try:
            self._engine = self._engine_factory()
        except Exception as exc:
            self._startup_error = exc
            self._startup_ready.set()
            return
        self._startup_ready.set()
        while True:
            command = self._commands.get()
            if command is None:
                return
            if command.generation != self._current_generation():
                command.loop.call_soon_threadsafe(self._resolve, command.future, None)
                continue
            try:
                self._engine.say(command.text)
                self._engine.runAndWait()
            except Exception as exc:
                command.loop.call_soon_threadsafe(self._resolve, command.future, exc)
            else:
                command.loop.call_soon_threadsafe(self._resolve, command.future, None)

    @staticmethod
    def _resolve(future: asyncio.Future[None], error: Exception | None) -> None:
        if future.done():
            return
        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)
