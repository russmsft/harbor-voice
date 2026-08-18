from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from harbor_voice.backends.transcription import FasterWhisperTranscriber
from harbor_voice.domain import AudioRecording


@dataclass
class Segment:
    text: str
    avg_logprob: float = -0.2


class FakeWhisper:
    def __init__(self) -> None:
        self.segments: list[Segment] = []
        self.calls: list[tuple[np.ndarray, dict]] = []

    def transcribe(self, audio: np.ndarray, **kwargs):
        self.calls.append((audio, kwargs))
        return iter(self.segments), object()


@pytest.mark.asyncio
async def test_transcriber_converts_pcm_and_joins_segments() -> None:
    fake = FakeWhisper()
    fake.segments = [Segment("  hello"), Segment(" world ")]
    transcriber = FasterWhisperTranscriber(model_factory=lambda: fake)
    recording = AudioRecording(pcm=b"\x00\x40\x00\xc0", sample_rate=16_000)

    transcript = await transcriber.transcribe(recording)

    assert transcript.text == "hello world"
    audio, kwargs = fake.calls[0]
    np.testing.assert_allclose(audio, np.array([0.5, -0.5], dtype=np.float32))
    assert kwargs == {"temperature": 0.0, "language": "en"}


@pytest.mark.asyncio
async def test_empty_segments_produce_empty_transcript() -> None:
    fake = FakeWhisper()
    fake.segments = [Segment("  ")]

    transcript = await FasterWhisperTranscriber(model_factory=lambda: fake).transcribe(
        AudioRecording(pcm=b"\x00\x00", sample_rate=16_000)
    )

    assert transcript.text == ""


@pytest.mark.asyncio
async def test_model_is_created_lazily_once() -> None:
    fake = FakeWhisper()
    calls = 0

    def factory() -> FakeWhisper:
        nonlocal calls
        calls += 1
        return fake

    transcriber = FasterWhisperTranscriber(model_factory=factory)
    recording = AudioRecording(pcm=b"\x00\x00", sample_rate=16_000)
    await transcriber.transcribe(recording)
    await transcriber.transcribe(recording)

    assert calls == 1

