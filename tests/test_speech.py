from __future__ import annotations

import asyncio
import threading

import pytest

from harbor_voice.backends.speech import SapiSpeaker, split_for_speech


def test_split_preserves_sentence_order() -> None:
    assert split_for_speech("One. Two? Three!") == ["One.", "Two?", "Three!"]


def test_split_caps_long_text_without_losing_words() -> None:
    text = "one two three four five six"

    segments = split_for_speech(text, max_chars=10)

    assert segments == ["one two", "three four", "five six"]
    assert " ".join(segments) == text


def test_split_ignores_blank_text() -> None:
    assert split_for_speech("   ") == []


class FakeSapiEngine:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.current = ""
        self.completed: list[str] = []
        self.stop_calls = 0

    def say(self, text: str) -> None:
        self.current = text

    def runAndWait(self) -> None:
        self.started.set()
        self.release.wait(timeout=2)
        if self.stop_calls == 0:
            self.completed.append(self.current)
        else:
            self.release.clear()
        self.started.clear()

    def stop(self) -> None:
        self.stop_calls += 1
        self.release.set()


@pytest.mark.asyncio
async def test_speaker_reads_all_segments_in_order() -> None:
    engine = FakeSapiEngine()
    engine.release.set()
    speaker = SapiSpeaker(engine_factory=lambda: engine)

    await speaker.speak("One. Two.")
    await speaker.close()

    assert engine.completed == ["One.", "Two."]


@pytest.mark.asyncio
async def test_cancel_stops_engine_and_discards_later_segments() -> None:
    engine = FakeSapiEngine()
    speaker = SapiSpeaker(engine_factory=lambda: engine)
    task = asyncio.create_task(speaker.speak("One. Two."))
    assert await asyncio.to_thread(engine.started.wait, 1)

    speaker.cancel()
    await asyncio.wait_for(task, timeout=1)
    await speaker.close()

    assert engine.stop_calls >= 1
    assert "Two." not in engine.completed


@pytest.mark.asyncio
async def test_speaker_can_be_reused_after_cancellation() -> None:
    engine = FakeSapiEngine()
    speaker = SapiSpeaker(engine_factory=lambda: engine)
    first = asyncio.create_task(speaker.speak("Interrupted."))
    assert await asyncio.to_thread(engine.started.wait, 1)
    speaker.cancel()
    await first
    engine.stop_calls = 0
    engine.release.set()

    await speaker.speak("Fresh response.")
    await speaker.close()

    assert engine.completed == ["Fresh response."]


@pytest.mark.asyncio
async def test_engine_initialization_failure_is_reported() -> None:
    def fail():
        raise RuntimeError("SAPI unavailable")

    speaker = SapiSpeaker(engine_factory=fail)

    with pytest.raises(RuntimeError, match="failed to initialize") as exc_info:
        await asyncio.wait_for(speaker.speak("Hello."), timeout=1)
    await speaker.close()

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "SAPI unavailable"
