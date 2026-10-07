"""Windows audio plumbing for meeting mode.

Meeting audio is heard through WASAPI loopback of the speaker device Teams plays to.
The agent speaks by writing synthesised speech into a virtual cable (VB-Audio "CABLE Input")
that Teams uses as its microphone. The user's real microphone is mixed into the same cable so
they can still talk normally. Nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import numpy as np

from harbor_voice.backends.speech import split_for_speech

BRIDGE_RATE = 48_000
_MIC_BUFFER_LIMIT = BRIDGE_RATE // 5  # keep passthrough latency under 200 ms

PcmCallback = Callable[[bytes, int, int], None]


class AudioDeviceNotFound(LookupError):
    """A configured meeting audio device is not present."""


def find_device(
    name: str | None,
    *,
    kind: str,
    hostapi: str = "Windows WASAPI",
    query_devices: Callable[[], Any] | None = None,
    query_hostapis: Callable[[int], Any] | None = None,
) -> int | None:
    """Return the sounddevice index whose name contains ``name`` on the preferred host API."""
    if name is None:
        return None
    if query_devices is None or query_hostapis is None:
        import sounddevice as sd

        query_devices = query_devices or sd.query_devices
        query_hostapis = query_hostapis or sd.query_hostapis
    channel_key = "max_input_channels" if kind == "input" else "max_output_channels"
    wanted = name.casefold()
    fallback: int | None = None
    for index, device in enumerate(query_devices()):
        if device[channel_key] <= 0 or wanted not in device["name"].casefold():
            continue
        if query_hostapis(device["hostapi"])["name"] == hostapi:
            return index
        if fallback is None:
            fallback = index
    if fallback is None:
        raise AudioDeviceNotFound(f"no {kind} audio device matches '{name}'")
    return fallback


class SapiPcmSynthesizer:
    """Synthesises speech to in-memory 48 kHz mono PCM with Windows SAPI."""

    def __init__(self, *, voice: str | None = None, rate: int = 0) -> None:
        self._voice_name = voice
        self._rate = rate
        self._jobs: queue.Queue[tuple[str, Future[bytes]] | None] = queue.Queue()
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="harbor-meeting-tts", daemon=True)
        self._thread.start()

    async def synthesize(self, text: str) -> bytes:
        await asyncio.to_thread(self._ready.wait)
        if self._startup_error is not None:
            raise RuntimeError("SAPI could not start") from self._startup_error
        future: Future[bytes] = Future()
        self._jobs.put((text, future))
        return await asyncio.wrap_future(future)

    def close(self) -> None:
        self._jobs.put(None)

    def _run(self) -> None:
        try:
            import comtypes
            import comtypes.client

            comtypes.CoInitialize()
            voice = comtypes.client.CreateObject("SAPI.SpVoice")
            from comtypes.gen import SpeechLib

            if self._voice_name:
                for token in voice.GetVoices():
                    if self._voice_name.casefold() in token.GetDescription().casefold():
                        voice.Voice = token
                        break
            voice.Rate = self._rate
            pcm_format = SpeechLib.SAFT48kHz16BitMono
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            job = self._jobs.get()
            if job is None:
                break
            text, future = job
            try:
                stream = comtypes.client.CreateObject("SAPI.SpMemoryStream")
                stream.Format.Type = pcm_format
                voice.AudioOutputStream = stream
                voice.Speak(text)
                future.set_result(bytes(stream.GetData()))
            except Exception as exc:
                future.set_exception(exc)
        with suppress(Exception):
            comtypes.CoUninitialize()


@dataclass(slots=True)
class _Playback:
    samples: np.ndarray
    position: int
    done: Callable[[], None]


class MeetingAudioBridge:
    """Mixes the user's microphone and agent speech into the virtual-cable output."""

    def __init__(
        self,
        *,
        output_device: int | None,
        mic_device: int | None,
        mic_passthrough: bool = True,
        on_mic_audio: PcmCallback | None = None,
        stream_module: Any = None,
    ) -> None:
        self._output_device = output_device
        self._mic_device = mic_device
        self._mic_passthrough = mic_passthrough
        self._on_mic_audio = on_mic_audio
        self._sd = stream_module
        self._lock = threading.Lock()
        self._mic = deque[np.ndarray]()
        self._mic_samples = 0
        self._queue: deque[_Playback] = deque()
        self._output = None
        self._input = None
        self._channels = 1

    @property
    def playing(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def start(self) -> None:
        sd = self._sd
        if sd is None:
            import sounddevice as sd

            self._sd = sd
        extra = _wasapi_settings(sd)
        output_device = _resolve_device(sd, self._output_device, "output")
        mic_device = _resolve_device(sd, self._mic_device, "input")
        info = sd.query_devices(output_device, "output")
        self._channels = 2 if info["max_output_channels"] >= 2 else 1
        self._output = sd.OutputStream(
            device=output_device,
            samplerate=BRIDGE_RATE,
            channels=self._channels,
            dtype="int16",
            callback=self._on_output,
            extra_settings=_settings_for(sd, output_device, extra),
        )
        self._input = sd.InputStream(
            device=mic_device,
            samplerate=BRIDGE_RATE,
            channels=1,
            dtype="int16",
            callback=self._on_input,
            extra_settings=_settings_for(sd, mic_device, extra),
        )
        try:
            self._output.start()
            self._input.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        for stream in (self._input, self._output):
            if stream is None:
                continue
            with suppress(Exception):
                stream.stop()
            with suppress(Exception):
                stream.close()
        self._input = None
        self._output = None
        self.clear()

    async def play(self, pcm: bytes) -> None:
        """Queue 48 kHz mono PCM and wait until it has been played or cleared."""
        samples = np.frombuffer(pcm, dtype=np.int16)
        if not len(samples):
            return
        loop = asyncio.get_running_loop()
        finished: asyncio.Future[None] = loop.create_future()

        def done() -> None:
            loop.call_soon_threadsafe(_resolve, finished)

        with self._lock:
            self._queue.append(_Playback(samples, 0, done))
        await finished

    def clear(self) -> None:
        with self._lock:
            pending = list(self._queue)
            self._queue.clear()
        for item in pending:
            item.done()

    def _on_input(self, indata, frames, time_info, status) -> None:
        del frames, time_info, status
        mono = np.array(indata[:, 0], dtype=np.int16)
        if self._mic_passthrough:
            with self._lock:
                self._mic.append(mono)
                self._mic_samples += len(mono)
                while self._mic_samples > _MIC_BUFFER_LIMIT and self._mic:
                    self._mic_samples -= len(self._mic.popleft())
        callback = self._on_mic_audio
        if callback is not None:
            callback(mono.tobytes(), BRIDGE_RATE, 1)

    def _on_output(self, outdata, frames, time_info, status) -> None:
        del time_info, status
        mix = np.zeros(frames, dtype=np.int32)
        finished: list[Callable[[], None]] = []
        with self._lock:
            self._drain_mic(mix, frames)
            self._drain_speech(mix, frames, finished)
        mixed = np.clip(mix, -32768, 32767).astype(np.int16)
        outdata[:] = mixed.reshape(-1, 1).repeat(self._channels, axis=1)
        for done in finished:
            done()

    def _drain_mic(self, mix: np.ndarray, frames: int) -> None:
        filled = 0
        while filled < frames and self._mic:
            chunk = self._mic[0]
            take = min(frames - filled, len(chunk))
            mix[filled : filled + take] += chunk[:take]
            filled += take
            self._mic_samples -= take
            if take == len(chunk):
                self._mic.popleft()
            else:
                self._mic[0] = chunk[take:]

    def _drain_speech(
        self,
        mix: np.ndarray,
        frames: int,
        finished: list[Callable[[], None]],
    ) -> None:
        filled = 0
        while filled < frames and self._queue:
            item = self._queue[0]
            take = min(frames - filled, len(item.samples) - item.position)
            mix[filled : filled + take] += item.samples[item.position : item.position + take]
            item.position += take
            filled += take
            if item.position >= len(item.samples):
                self._queue.popleft()
                finished.append(item.done)


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


def _wasapi_settings(sd: Any) -> Any:
    factory = getattr(sd, "WasapiSettings", None)
    if factory is None:
        return None
    try:
        return factory(auto_convert=True)
    except TypeError:
        return None


def _wasapi_index(sd: Any) -> int | None:
    for index, api in enumerate(sd.query_hostapis()):
        if api["name"] == "Windows WASAPI":
            return index
    return None


def _resolve_device(sd: Any, device: int | None, kind: str) -> int | None:
    """Default devices resolve to the WASAPI default so resampling settings apply."""
    if device is not None:
        return device
    api = _wasapi_index(sd)
    if api is None:
        return None
    index = sd.query_hostapis(api)[f"default_{kind}_device"]
    return index if index >= 0 else None


def _settings_for(sd: Any, device: int | None, extra: Any) -> Any:
    if extra is None or device is None:
        return None
    return extra if sd.query_devices(device)["hostapi"] == _wasapi_index(sd) else None


class MeetingSpeaker:
    """Speaker port that talks into the meeting through the audio bridge."""

    def __init__(self, synthesizer: SapiPcmSynthesizer, bridge: MeetingAudioBridge) -> None:
        self._synthesizer = synthesizer
        self._bridge = bridge
        self._generation = 0

    async def speak(self, text: str) -> None:
        generation = self._generation
        for segment in split_for_speech(text):
            pcm = await self._synthesizer.synthesize(segment)
            if generation != self._generation:
                return
            await self._bridge.play(pcm)
            if generation != self._generation:
                return

    def cancel(self) -> None:
        self._generation += 1
        self._bridge.clear()

    async def close(self) -> None:
        self.cancel()
        self._synthesizer.close()


class LoopbackListener:
    """Captures what the meeting plays on a speaker device via WASAPI loopback."""

    def __init__(self, on_audio: PcmCallback, *, device_name: str | None = None) -> None:
        self._on_audio = on_audio
        self._device_name = device_name
        self._pa = None
        self._stream = None
        self.device_label = ""

    def start(self) -> None:
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        try:
            device = self._select_device(pa)
            channels = max(1, int(device["maxInputChannels"]))
            rate = int(device["defaultSampleRate"])

            def callback(in_data, frame_count, time_info, status):
                del frame_count, time_info, status
                self._on_audio(in_data, rate, channels)
                return (None, pyaudio.paContinue)

            self._stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=rate // 50,
                stream_callback=callback,
            )
            self._stream.start_stream()
        except Exception:
            pa.terminate()
            raise
        self._pa = pa
        self.device_label = device["name"]

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with suppress(Exception):
                stream.stop_stream()
            with suppress(Exception):
                stream.close()
        pa, self._pa = self._pa, None
        if pa is not None:
            with suppress(Exception):
                pa.terminate()

    def _select_device(self, pa) -> dict:
        if not self._device_name:
            return pa.get_default_wasapi_loopback()
        wanted = self._device_name.casefold()
        for device in pa.get_loopback_device_info_generator():
            if wanted in device["name"].casefold():
                return device
        raise AudioDeviceNotFound(f"no loopback device matches '{self._device_name}'")
