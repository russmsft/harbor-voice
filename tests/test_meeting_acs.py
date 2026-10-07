from __future__ import annotations

import asyncio
import json
import struct
from datetime import datetime
from types import SimpleNamespace

import pytest

from harbor_voice.app import is_teams_meeting_link
from harbor_voice.domain import AudioRecording, MessageResponse, Transcript
from harbor_voice.meeting.acs.azure import AcsTokenProvider, AzureNeuralSynthesizer
from harbor_voice.meeting.acs.transport import AcsMeetingTransport
from harbor_voice.meeting.agent import MeetingAgent, MeetingAwareBackend, MeetingConfig
from harbor_voice.meeting.session import AzureMeetingOptions, MeetingDevices, MeetingSession
from harbor_voice.meeting.transcript import PARTICIPANT, is_bare_wake
from tests.fakes import FakeBackend, FakeSpeaker


class ScriptedTranscriber:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)

    async def transcribe(self, recording: AudioRecording) -> Transcript:
        del recording
        return Transcript(self.texts.pop(0))


def recording() -> AudioRecording:
    return AudioRecording(b"\x00\x00" * 1_600, 16_000)


# --- agent conversation improvements ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Scout.", True), ("Hey Scout", True), ("Scout, what time is it?", False), ("Hi", False)],
)
def test_bare_wake_detection(text: str, expected: bool) -> None:
    assert is_bare_wake(text, ["scout"]) is expected


@pytest.mark.asyncio
async def test_bare_wake_acknowledges_then_answers_follow_up() -> None:
    events: list[str] = []
    backend = FakeBackend(events, MessageResponse(kind="message", message="Lots of things."))
    speaker = FakeSpeaker(events)
    agent = MeetingAgent(
        backend=backend,
        speaker=speaker,
        transcriber=ScriptedTranscriber("Scout.", "What can you do?"),
    )

    await agent.handle_utterance(PARTICIPANT, recording())
    await agent.wait_idle()
    await agent.handle_utterance(PARTICIPANT, recording())
    await agent.wait_idle()

    assert speaker.spoken == ["Yes?", "Lots of things."]
    assert "What can you do?" in backend.requests[0].text


@pytest.mark.asyncio
async def test_follow_up_window_expires() -> None:
    now = [0.0]
    events: list[str] = []
    backend = FakeBackend(events)
    agent = MeetingAgent(
        backend=backend,
        speaker=FakeSpeaker(events),
        transcriber=ScriptedTranscriber("Scout.", "Anyway, moving on."),
        clock=lambda: now[0],
    )

    await agent.handle_utterance(PARTICIPANT, recording())
    now[0] = 60.0
    await agent.handle_utterance(PARTICIPANT, recording())
    await agent.wait_idle()

    assert backend.requests == []


@pytest.mark.asyncio
async def test_prompt_includes_current_time() -> None:
    events: list[str] = []
    backend = FakeBackend(events)
    agent = MeetingAgent(
        backend=backend,
        speaker=FakeSpeaker(events),
        transcriber=ScriptedTranscriber(),
        now=lambda: datetime(2026, 10, 7, 14, 32),
    )

    await agent.respond("Scout, what time is it?", asker=PARTICIPANT)

    assert "Wednesday 07 October 2026, 14:32" in backend.requests[0].text


# --- Azure services -----------------------------------------------------------------------


class FakeCredential:
    def __init__(self) -> None:
        self.calls = 0

    def get_token(self, scope: str):
        self.calls += 1
        return SimpleNamespace(token=f"token-{self.calls}", expires_on=10_000)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


@pytest.mark.asyncio
async def test_neural_synthesizer_escapes_text_and_reuses_token() -> None:
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(b"\x01\x00" * 10)

    credential = FakeCredential()
    synthesizer = AzureNeuralSynthesizer(
        "https://speech.example/",
        voice="en-GB-SoniaNeural",
        credential=credential,
        opener=opener,
        clock=lambda: 0,
    )

    pcm = await synthesizer.synthesize("Fish & <chips>")
    await synthesizer.synthesize("again")

    assert pcm == b"\x01\x00" * 10
    assert credential.calls == 1
    assert requests[0].full_url == "https://speech.example/tts/cognitiveservices/v1"
    body = requests[0].data.decode()
    assert "Fish &amp; &lt;chips&gt;" in body and "xml:lang='en-GB'" in body
    assert requests[0].get_header("Authorization") == "Bearer token-1"
    assert requests[0].get_header("X-microsoft-outputformat") == "raw-48khz-16bit-mono-pcm"


@pytest.mark.asyncio
async def test_acs_token_provider_requests_voip_scope() -> None:
    seen = {}

    class Client:
        def create_user_and_token(self, scopes):
            seen["scopes"] = scopes
            return object(), SimpleNamespace(token="acs-token")

    provider = AcsTokenProvider(
        "https://acs.example",
        credential=object(),
        client_factory=lambda endpoint, credential: Client(),
    )

    assert await provider.issue() == "acs-token"
    assert [str(scope.value) for scope in seen["scopes"]] == ["voip"]


# --- ACS transport over a real loopback WebSocket -----------------------------------------


class PageSimulator:
    """Stands in for headless Edge: fetches the page and connects like bridge.js does."""

    def __init__(self) -> None:
        self.url = ""
        self.received: list[object] = []
        self.task: asyncio.Task | None = None
        self.closed = False

    def launch(self, url: str) -> None:
        self.url = url
        self.task = asyncio.get_event_loop().create_task(self._run())

    def close(self) -> None:
        self.closed = True

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        base, query = self.url.split("/?", 1)
        async with connect(f"{base.replace('http', 'ws')}/bridge?{query}") as socket:
            self.socket = socket
            await socket.send(json.dumps({"type": "status", "state": "ready"}))
            async for message in socket:
                self.received.append(message)
                if isinstance(message, bytes):
                    identifier = struct.unpack("<I", message[:4])[0]
                    await socket.send(json.dumps({"type": "played", "id": identifier}))
                elif json.loads(message)["type"] == "join":
                    await socket.send(json.dumps({"type": "status", "state": "InLobby"}))
                    await socket.send(b"\x05\x00" * 480)
                elif json.loads(message)["type"] == "leave":
                    return


class StaticToken:
    async def issue(self) -> str:
        return "acs-token"


@pytest.mark.asyncio
async def test_transport_joins_relays_audio_and_acknowledges_playback() -> None:
    statuses: list[tuple[str, str]] = []
    audio: list[bytes] = []
    page = PageSimulator()
    transport = AcsMeetingTransport(
        meeting_link="https://teams.microsoft.com/l/meetup-join/abc",
        display_name="Scout (AI)",
        token_provider=StaticToken(),
        on_participant_audio=lambda pcm, rate, channels: audio.append(pcm),
        on_status=lambda state, detail: statuses.append((state, detail)),
        launcher=page,
        connect_timeout=5,
    )

    await transport.start()
    await asyncio.wait_for(transport.play(b"\x01\x00" * 100), 5)
    for _ in range(50):
        if audio and ("InLobby", "") in statuses:
            break
        await asyncio.sleep(0.02)
    await transport.stop()

    join = json.loads(page.received[0])
    assert join == {
        "type": "join",
        "token": "acs-token",
        "meetingLink": "https://teams.microsoft.com/l/meetup-join/abc",
        "displayName": "Scout (AI)",
    }
    assert page.url.startswith("http://localhost:")
    assert audio == [b"\x05\x00" * 480]
    assert ("InLobby", "") in statuses
    assert page.closed


@pytest.mark.asyncio
async def test_transport_rejects_connections_without_the_nonce() -> None:
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    class NoPage:
        url = ""

        def launch(self, url: str) -> None:
            self.url = url

        def close(self) -> None:
            pass

    launcher = NoPage()
    transport = AcsMeetingTransport(
        meeting_link="x",
        display_name="Scout (AI)",
        token_provider=StaticToken(),
        on_participant_audio=lambda *args: None,
        on_status=lambda *args: None,
        launcher=launcher,
        connect_timeout=0.5,
    )
    with pytest.raises(RuntimeError):
        await transport.start()
    assert launcher.url  # page was launched before the timeout

    # A fresh transport keeps the server alive long enough to probe it.
    transport = AcsMeetingTransport(
        meeting_link="x",
        display_name="Scout (AI)",
        token_provider=StaticToken(),
        on_participant_audio=lambda *args: None,
        on_status=lambda *args: None,
        launcher=launcher,
        connect_timeout=3,
    )
    starting = asyncio.create_task(transport.start())
    for _ in range(100):
        if transport._port:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(InvalidStatus):
        async with connect(f"ws://localhost:{transport._port}/bridge?nonce=wrong"):
            pass
    with pytest.raises(RuntimeError):
        await starting


# --- remote session -----------------------------------------------------------------------


class FakeTransport:
    def __init__(self, on_participant_audio, on_status) -> None:
        self.on_audio = on_participant_audio
        self.on_status = on_status
        self.device_label = "the Teams meeting as Scout (AI)"
        self.stopped = False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_remote_session_announces_after_admission_and_ends_on_disconnect() -> None:
    events: list[str] = []
    speaker = FakeSpeaker(events)
    transports: list[FakeTransport] = []
    notices = []
    session = MeetingSession(
        backend=MeetingAwareBackend(FakeBackend(events)),
        transcriber=ScriptedTranscriber(),
        config=MeetingConfig(),
        devices=MeetingDevices(),
        on_event=notices.append,
        azure=AzureMeetingOptions(meeting_link="https://teams.microsoft.com/l/meetup-join/x"),
        speaker_factory=lambda bridge: speaker,
        transport_factory=lambda **kwargs: (
            transports.append(FakeTransport(**kwargs)) or transports[-1]
        ),
    )

    await session.start()
    transports[0].on_status("InLobby", "")
    await asyncio.sleep(0.01)
    assert speaker.spoken == []
    transports[0].on_status("Connected", "")
    await asyncio.sleep(0.01)
    assert "Scout" in speaker.spoken[0]
    session.suppress_participants(True)
    assert session._participants.suppressed is False
    transports[0].on_status("Disconnected", "0/0")
    await asyncio.sleep(0.01)
    await session.stop()

    kinds = [notice.kind for notice in notices]
    assert "ended" in kinds
    assert any("lobby" in notice.text for notice in notices)
    assert transports[0].stopped


def test_remote_session_requires_acs_endpoint() -> None:
    session = MeetingSession(
        backend=FakeBackend([]),
        transcriber=ScriptedTranscriber(),
        config=MeetingConfig(),
        devices=MeetingDevices(),
        on_event=lambda event: None,
        azure=AzureMeetingOptions(meeting_link="https://teams.microsoft.com/l/x"),
    )
    with pytest.raises(ValueError):
        session._make_transport()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://teams.microsoft.com/l/meetup-join/19%3ameeting", True),
        ("  https://teams.live.com/meet/123  ", True),
        ("http://teams.microsoft.com/l/meetup-join/x", False),
        ("https://example.com/teams.microsoft.com/", False),
    ],
)
def test_teams_meeting_link_validation(text: str, expected: bool) -> None:
    assert is_teams_meeting_link(text) is expected
