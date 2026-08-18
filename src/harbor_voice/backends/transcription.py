"""Local faster-whisper transcription adapter."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from harbor_voice.domain import AudioRecording, Transcript


class FasterWhisperTranscriber:
    def __init__(
        self,
        *,
        model_name: str = "base.en",
        device: str = "auto",
        compute_type: str = "int8",
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model_factory = model_factory or self._create_model
        self._model = None
        self._model_lock = threading.Lock()

    async def transcribe(self, recording: AudioRecording) -> Transcript:
        return await asyncio.to_thread(self._transcribe_sync, recording)

    def _transcribe_sync(self, recording: AudioRecording) -> Transcript:
        model = self._get_model()
        samples = np.frombuffer(recording.pcm, dtype=np.int16).astype(np.float32)
        samples /= 32768.0
        language = "en" if self.model_name.endswith(".en") else None
        segments, _ = model.transcribe(samples, temperature=0.0, language=language)
        text = "".join(segment.text for segment in segments).strip()
        return Transcript(text=text)

    def _get_model(self):
        with self._model_lock:
            if self._model is None:
                self._model = self._model_factory()
        return self._model

    def _create_model(self):
        from faster_whisper import WhisperModel

        return WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
        )

