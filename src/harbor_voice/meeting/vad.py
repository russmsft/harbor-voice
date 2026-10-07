"""Streaming PCM helpers: resampling and energy-based utterance segmentation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

import numpy as np

TARGET_RATE = 16_000


def to_mono_16k(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """Convert interleaved 16-bit PCM to 16 kHz mono 16-bit PCM."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    if channels > 1:
        usable = len(samples) - (len(samples) % channels)
        samples = samples[:usable].reshape(-1, channels).astype(np.float32).mean(axis=1)
    else:
        samples = samples.astype(np.float32)
    if sample_rate != TARGET_RATE and len(samples):
        target_length = max(1, int(round(len(samples) * TARGET_RATE / sample_rate)))
        positions = np.linspace(0, len(samples) - 1, target_length)
        samples = np.interp(positions, np.arange(len(samples)), samples)
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


def rms(frame: bytes) -> float:
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    if not len(samples):
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


class EnergySegmenter:
    """Split a continuous 16 kHz mono stream into speech utterances.

    The threshold adapts to the background level so quiet rooms and noisy calls both work.
    Audio only lives in memory and is discarded once an utterance is emitted.
    """

    def __init__(
        self,
        *,
        frame_ms: int = 30,
        absolute_floor: float = 250.0,
        noise_multiplier: float = 3.0,
        min_speech_ms: int = 350,
        end_silence_ms: int = 700,
        max_utterance_ms: int = 20_000,
        preroll_ms: int = 240,
        sustained_ms: int = 800,
        on_sustained_speech: Callable[[], None] | None = None,
    ) -> None:
        self._frame_bytes = TARGET_RATE * frame_ms // 1000 * 2
        self._frame_ms = frame_ms
        self._floor = absolute_floor
        self._multiplier = noise_multiplier
        self._min_frames = max(1, min_speech_ms // frame_ms)
        self._end_frames = max(1, end_silence_ms // frame_ms)
        self._max_frames = max(1, max_utterance_ms // frame_ms)
        self._sustained_frames = max(1, sustained_ms // frame_ms)
        self._on_sustained = on_sustained_speech
        self._pending = bytearray()
        self._preroll: deque[bytes] = deque(maxlen=max(1, preroll_ms // frame_ms))
        self._noise = absolute_floor / noise_multiplier
        self._speech: list[bytes] = []
        self._voiced_frames = 0
        self._silent_run = 0
        self._sustained_fired = False
        self.suppressed = False

    @property
    def threshold(self) -> float:
        return max(self._floor, self._noise * self._multiplier)

    @property
    def in_speech(self) -> bool:
        return bool(self._speech)

    def reset(self) -> None:
        self._pending.clear()
        self._preroll.clear()
        self._speech = []
        self._voiced_frames = 0
        self._silent_run = 0
        self._sustained_fired = False

    def feed(self, pcm: bytes) -> list[bytes]:
        """Consume 16 kHz mono PCM and return any completed utterances."""
        if self.suppressed:
            self.reset()
            return []
        self._pending.extend(pcm)
        completed: list[bytes] = []
        while len(self._pending) >= self._frame_bytes:
            frame = bytes(self._pending[: self._frame_bytes])
            del self._pending[: self._frame_bytes]
            utterance = self._process(frame)
            if utterance is not None:
                completed.append(utterance)
        return completed

    def flush(self) -> bytes | None:
        utterance = self._finish()
        self.reset()
        return utterance

    def _process(self, frame: bytes) -> bytes | None:
        level = rms(frame)
        voiced = level >= self.threshold
        if not self._speech:
            if voiced:
                self._speech = [*self._preroll, frame]
                self._voiced_frames = 1
                self._silent_run = 0
                self._sustained_fired = False
            else:
                self._noise = 0.95 * self._noise + 0.05 * level
                self._preroll.append(frame)
            return None

        self._speech.append(frame)
        if voiced:
            self._voiced_frames += 1
            self._silent_run = 0
            if (
                not self._sustained_fired
                and self._voiced_frames >= self._sustained_frames
                and self._on_sustained is not None
            ):
                self._sustained_fired = True
                self._on_sustained()
        else:
            self._silent_run += 1
        if self._silent_run >= self._end_frames or len(self._speech) >= self._max_frames:
            return self._finish()
        return None

    def _finish(self) -> bytes | None:
        speech, voiced = self._speech, self._voiced_frames
        self._speech = []
        self._voiced_frames = 0
        self._silent_run = 0
        self._preroll.clear()
        if not speech or voiced < self._min_frames:
            return None
        return b"".join(speech)
