"""In-memory mono PCM microphone capture."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from harbor_voice.domain import AudioRecording


class RecorderBusy(RuntimeError):
    """A second capture was requested before the first one stopped."""


def _sounddevice_stream_factory(**kwargs):
    import sounddevice as sd

    return sd.RawInputStream(**kwargs)


class MemoryRecorder:
    def __init__(
        self,
        stream_factory: Callable[..., Any] = _sounddevice_stream_factory,
        *,
        min_duration_ms: int = 250,
        device: int | str | None = None,
    ) -> None:
        self._stream_factory = stream_factory
        self.min_duration_ms = min_duration_ms
        self.device = device
        self._stream = None
        self._pcm = bytearray()
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            raise RecorderBusy("microphone capture is already active")
        with self._lock:
            self._pcm.clear()
        stream = self._stream_factory(
            samplerate=16_000,
            channels=1,
            dtype="int16",
            device=self.device,
            callback=self._on_audio,
        )
        self._stream = stream
        stream.start()

    def stop(self) -> AudioRecording | None:
        stream, self._stream = self._stream, None
        if stream is None:
            return None
        stream.stop()
        stream.close()
        with self._lock:
            pcm = bytes(self._pcm)
            self._pcm.clear()
        duration_ms = len(pcm) * 1_000 // (16_000 * 2)
        if duration_ms < self.min_duration_ms:
            return None
        return AudioRecording(pcm=pcm, sample_rate=16_000)

    def close(self) -> None:
        if self._stream is not None:
            self.stop()

    def _on_audio(self, indata, frames, time_info, status) -> None:
        del frames, time_info, status
        with self._lock:
            self._pcm.extend(bytes(indata))

