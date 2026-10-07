"""Runtime wiring for one live meeting: audio in, transcription queue, agent, audio out."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from harbor_voice.domain import AudioRecording
from harbor_voice.meeting.agent import MeetingAgent, MeetingConfig, MeetingEvent
from harbor_voice.meeting.transcript import ME, PARTICIPANT
from harbor_voice.meeting.vad import TARGET_RATE, EnergySegmenter, to_mono_16k

_QUEUE_LIMIT = 32


@dataclass(frozen=True, slots=True)
class MeetingDevices:
    loopback_device: str | None = None
    output_device: str | None = "CABLE Input"
    mic_device: int | str | None = None
    mic_passthrough: bool = True
    voice: str | None = None


@dataclass(frozen=True, slots=True)
class AzureMeetingOptions:
    """Azure services for the remote participant and, optionally, neural speech."""

    meeting_link: str | None = None
    display_name: str = "Scout (AI)"
    acs_endpoint: str | None = None
    speech_endpoint: str | None = None
    neural_voice: str = "en-GB-SoniaNeural"
    tenant_id: str | None = None
    show_browser: bool = False

    @property
    def remote(self) -> bool:
        return bool(self.meeting_link)


class MeetingSession:
    def __init__(
        self,
        *,
        backend: Any,
        transcriber: Any,
        config: MeetingConfig,
        devices: MeetingDevices,
        on_event: Callable[[MeetingEvent], None],
        azure: AzureMeetingOptions | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        bridge_factory: Callable[..., Any] | None = None,
        listener_factory: Callable[..., Any] | None = None,
        speaker_factory: Callable[[Any], Any] | None = None,
        transport_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._backend = backend
        self._transcriber = transcriber
        self._config = config
        self._devices = devices
        self._azure = azure or AzureMeetingOptions()
        self._on_event = on_event
        self._loop = loop
        self._bridge_factory = bridge_factory
        self._listener_factory = listener_factory
        self._speaker_factory = speaker_factory
        self._transport_factory = transport_factory
        self._queue: asyncio.Queue[tuple[str, AudioRecording]] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._bridge = None
        self._listener = None
        self._speaker = None
        self._announced = False
        self._participants = EnergySegmenter(on_sustained_speech=self._participant_barge_in)
        self._me = EnergySegmenter()
        self.agent: MeetingAgent | None = None
        self.active = False

    @property
    def remote(self) -> bool:
        return self._azure.remote

    async def start(self) -> None:
        if self.active:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._announced = False
        bridge = self._make_transport() if self.remote else self._make_bridge()
        speaker = self._make_speaker(bridge)
        self.agent = MeetingAgent(
            backend=self._backend,
            speaker=speaker,
            transcriber=self._transcriber,
            config=self._config,
            on_event=self._on_event,
        )
        self._speaker = speaker
        # Accept audio as soon as the transport starts; the remote join completes later.
        self.active = True
        try:
            await _maybe_await(bridge.start())
            self._bridge = bridge
            if self.remote:
                label = bridge.device_label
            else:
                listener = self._make_listener()
                listener.start()
                self._listener = listener
                label = getattr(listener, "device_label", "") or "default speakers"
        except BaseException:
            self.active = False
            self._bridge = bridge
            await self._teardown(speaker)
            self._speaker = None
            raise
        self._set_context_provider(self.agent.private_context)
        self._worker = self._loop.create_task(self._drain())
        if self.remote:
            self._on_event(MeetingEvent("status", f"Joining {label}…"))
        else:
            self._on_event(MeetingEvent("status", f"Meeting mode on. Listening to {label}."))
            self._announce()

    async def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        if self.agent is not None:
            self.agent.interrupt()
            self.agent.cancel_pending()
            await self.agent.wait_idle()
        await self._teardown(self._speaker)
        self._speaker = None
        self._set_context_provider(None)
        if self.agent is not None:
            self.agent.clear()
        self._participants.reset()
        self._me.reset()
        self._on_event(MeetingEvent("status", "Meeting mode off. Meeting transcript discarded."))

    def suppress_participants(self, suppressed: bool) -> None:
        """Ignore loopback audio, e.g. while a private reply plays on the speakers."""
        if not self.remote:
            self._participants.suppressed = suppressed

    async def answer_latest(self) -> None:
        if self.agent is not None and self.active:
            await self.agent.answer_latest()

    def interrupt(self) -> None:
        if self.agent is not None:
            self.agent.interrupt()

    def _make_bridge(self):
        if self._bridge_factory is not None:
            return self._bridge_factory(on_mic_audio=self._on_mic_audio)
        from harbor_voice.meeting.audio import MeetingAudioBridge, find_device

        output = find_device(self._devices.output_device, kind="output")
        mic = self._devices.mic_device
        if isinstance(mic, str):
            mic = find_device(mic, kind="input")
        return MeetingAudioBridge(
            output_device=output,
            mic_device=mic,
            mic_passthrough=self._devices.mic_passthrough,
            on_mic_audio=self._on_mic_audio,
        )

    def _make_speaker(self, bridge):
        if self._speaker_factory is not None:
            return self._speaker_factory(bridge)
        from harbor_voice.meeting.audio import MeetingSpeaker, SapiPcmSynthesizer

        if self._azure.speech_endpoint:
            from harbor_voice.meeting.acs.azure import AzureNeuralSynthesizer

            synthesizer = AzureNeuralSynthesizer(
                self._azure.speech_endpoint,
                voice=self._azure.neural_voice,
                tenant_id=self._azure.tenant_id,
            )
        else:
            synthesizer = SapiPcmSynthesizer(voice=self._devices.voice)
        return MeetingSpeaker(synthesizer, bridge)

    def _make_transport(self):
        if self._transport_factory is not None:
            return self._transport_factory(
                on_participant_audio=self._on_loopback_audio,
                on_status=self._on_transport_status,
            )
        if not self._azure.acs_endpoint:
            raise ValueError("meeting_acs_endpoint must be set to send Scout to a meeting")
        from harbor_voice.meeting.acs.azure import AcsTokenProvider
        from harbor_voice.meeting.acs.transport import AcsMeetingTransport, EdgeLauncher

        return AcsMeetingTransport(
            meeting_link=self._azure.meeting_link or "",
            display_name=self._azure.display_name,
            token_provider=AcsTokenProvider(
                self._azure.acs_endpoint, tenant_id=self._azure.tenant_id
            ),
            on_participant_audio=self._on_loopback_audio,
            on_status=self._on_transport_status,
            launcher=EdgeLauncher(headless=not self._azure.show_browser),
        )

    def _make_listener(self):
        if self._listener_factory is not None:
            return self._listener_factory(self._on_loopback_audio)
        from harbor_voice.meeting.audio import LoopbackListener

        return LoopbackListener(self._on_loopback_audio, device_name=self._devices.loopback_device)

    async def _teardown(self, speaker) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            with suppress(Exception):
                listener.stop()
        if speaker is not None:
            with suppress(Exception):
                await speaker.close()
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            with suppress(Exception):
                await _maybe_await(bridge.stop())

    def _announce(self) -> None:
        if self._announced or self.agent is None or self._loop is None:
            return
        self._announced = True
        self._loop.create_task(self._safe(self.agent.announce()))

    # Remote participant status arrives on the bridge thread; hop to the event loop.
    def _on_transport_status(self, state: str, detail: str) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._handle_transport_status, state, detail)

    def _handle_transport_status(self, state: str, detail: str) -> None:
        if not self.active:
            return
        name = self._azure.display_name
        if state == "InLobby":
            self._on_event(MeetingEvent("status", f"{name} is in the lobby. Admit it from Teams."))
        elif state == "Connected":
            self._on_event(MeetingEvent("status", f"{name} joined the meeting."))
            self._announce()
        elif state == "muted":
            self._on_event(
                MeetingEvent("error", f"{name} is muted by the meeting, so it can't speak.")
            )
        elif state == "error":
            self._on_event(MeetingEvent("error", f"{name}: {detail}"))
        elif state in {"Disconnected", "closed"}:
            suffix = f" ({detail})" if detail and detail != "0/0" else ""
            self._on_event(MeetingEvent("ended", f"{name} left the meeting{suffix}."))

    def _set_context_provider(self, provider) -> None:
        if hasattr(self._backend, "context_provider"):
            self._backend.context_provider = provider

    # Audio callbacks arrive on PortAudio threads; hop to the event loop.
    def _on_loopback_audio(self, pcm: bytes, rate: int, channels: int) -> None:
        self._feed(self._participants, PARTICIPANT, pcm, rate, channels)

    def _on_mic_audio(self, pcm: bytes, rate: int, channels: int) -> None:
        self._feed(self._me, ME, pcm, rate, channels)

    def _feed(self, segmenter, label, pcm, rate, channels) -> None:
        if not self.active:
            return
        for utterance in segmenter.feed(to_mono_16k(pcm, rate, channels)):
            recording = AudioRecording(pcm=utterance, sample_rate=TARGET_RATE)
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._enqueue, label, recording)

    def _participant_barge_in(self) -> None:
        loop, agent = self._loop, self.agent
        if loop is not None and agent is not None:
            loop.call_soon_threadsafe(agent.on_participant_speech)

    def _enqueue(self, label: str, recording: AudioRecording) -> None:
        queue = self._queue
        if queue is None or not self.active:
            return
        if queue.full():
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        queue.put_nowait((label, recording))

    async def _drain(self) -> None:
        assert self._queue is not None and self.agent is not None
        while True:
            label, recording = await self._queue.get()
            await self._safe(self.agent.handle_utterance(label, recording))

    async def _safe(self, operation) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._on_event(MeetingEvent("error", str(exc) or type(exc).__name__))


async def _maybe_await(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result
