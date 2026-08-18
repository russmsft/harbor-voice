from __future__ import annotations

from pathlib import Path

import pytest

from harbor_voice.audio.capture import MemoryRecorder, RecorderBusy
from harbor_voice.audio.hotkey import GlobalHoldKey


def pcm_ms(milliseconds: int, sample_rate: int = 16_000) -> bytes:
    return b"\x00\x00" * (sample_rate * milliseconds // 1_000)


class FakeStream:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def feed(self, data: bytes) -> None:
        self.callback(data, len(data) // 2, None, None)


class FakeStreamFactory:
    def __init__(self) -> None:
        self.stream: FakeStream | None = None
        self.kwargs: dict = {}

    def __call__(self, *, callback, **kwargs) -> FakeStream:
        self.kwargs = kwargs
        self.stream = FakeStream(callback)
        return self.stream

    def feed(self, data: bytes) -> None:
        assert self.stream is not None
        self.stream.feed(data)


def test_short_recording_is_ignored() -> None:
    factory = FakeStreamFactory()
    recorder = MemoryRecorder(factory, min_duration_ms=250)
    recorder.start()
    factory.feed(pcm_ms(200))

    assert recorder.stop() is None


def test_recording_stays_in_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    factory = FakeStreamFactory()
    recorder = MemoryRecorder(factory, min_duration_ms=1)
    recorder.start()
    factory.feed(pcm_ms(30))

    recording = recorder.stop()

    assert recording is not None
    assert recording.pcm == pcm_ms(30)
    assert set(tmp_path.iterdir()) == before


def test_recorder_requests_mono_sixteen_kilohertz_pcm() -> None:
    factory = FakeStreamFactory()
    recorder = MemoryRecorder(factory)

    recorder.start()

    assert factory.kwargs == {
        "samplerate": 16_000,
        "channels": 1,
        "dtype": "int16",
        "device": None,
    }


def test_recorder_rejects_overlapping_capture() -> None:
    recorder = MemoryRecorder(FakeStreamFactory())
    recorder.start()

    with pytest.raises(RecorderBusy):
        recorder.start()


class FakeListener:
    def __init__(self, on_press, on_release) -> None:
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_hotkey_emits_once_per_physical_hold() -> None:
    events: list[str] = []
    created: list[FakeListener] = []

    def factory(on_press, on_release) -> FakeListener:
        listener = FakeListener(on_press, on_release)
        created.append(listener)
        return listener

    hotkey = GlobalHoldKey("f9", events.append, events.append, listener_factory=factory)
    hotkey.start()
    listener = created[0]
    listener.on_press("f9")
    listener.on_press("f9")
    listener.on_release("f9")
    listener.on_release("f9")

    assert events == ["pressed", "released"]


def test_hotkey_ignores_other_keys() -> None:
    events: list[str] = []
    created: list[FakeListener] = []

    def factory(on_press, on_release) -> FakeListener:
        listener = FakeListener(on_press, on_release)
        created.append(listener)
        return listener

    hotkey = GlobalHoldKey("f9", events.append, events.append, listener_factory=factory)
    hotkey.start()
    created[0].on_press("f8")
    created[0].on_release("f8")

    assert events == []

