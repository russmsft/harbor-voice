"""Microsoft Entra-authenticated Azure services used by the ACS meeting participant."""

from __future__ import annotations

import asyncio
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any
from xml.sax.saxutils import escape

_COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


def default_credential(tenant_id: str | None = None) -> Any:
    """Use the developer's Azure CLI sign-in, then other non-interactive sources."""
    from azure.identity import AzureCliCredential, ChainedTokenCredential, DefaultAzureCredential

    return ChainedTokenCredential(
        AzureCliCredential(tenant_id=tenant_id),
        DefaultAzureCredential(
            exclude_cli_credential=True,
            exclude_interactive_browser_credential=True,
        ),
    )


class AcsTokenProvider:
    """Issues a short-lived VoIP identity and token for one meeting join."""

    def __init__(
        self,
        endpoint: str,
        *,
        credential: Any = None,
        tenant_id: str | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._credential = credential
        self._tenant_id = tenant_id
        self._client_factory = client_factory

    async def issue(self) -> str:
        return await asyncio.to_thread(self._issue_sync)

    def _issue_sync(self) -> str:
        credential = self._credential or default_credential(self._tenant_id)
        if self._client_factory is not None:
            client = self._client_factory(self._endpoint, credential)
        else:
            from azure.communication.identity import CommunicationIdentityClient

            client = CommunicationIdentityClient(self._endpoint, credential)
        from azure.communication.identity import CommunicationTokenScope

        _, token = client.create_user_and_token(scopes=[CommunicationTokenScope.VOIP])
        return token.token


class AzureNeuralSynthesizer:
    """Azure AI Speech neural text-to-speech returning 48 kHz mono 16-bit PCM in memory."""

    def __init__(
        self,
        endpoint: str,
        *,
        voice: str = "en-GB-SoniaNeural",
        credential: Any = None,
        tenant_id: str | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._url = endpoint.rstrip("/") + "/tts/cognitiveservices/v1"
        self.voice = voice
        self._credential = credential
        self._tenant_id = tenant_id
        self._opener = opener
        self._clock = clock
        self._token: Any = None
        self._lock = threading.Lock()

    async def synthesize(self, text: str) -> bytes:
        return await asyncio.to_thread(self._synthesize_sync, text)

    def close(self) -> None:
        self._token = None

    def ssml(self, text: str) -> str:
        language = "-".join(self.voice.split("-")[:2])
        return (
            f"<speak version='1.0' xml:lang='{language}'>"
            f"<voice name='{escape(self.voice, {chr(39): '&apos;'})}'>{escape(text)}</voice>"
            "</speak>"
        )

    def _synthesize_sync(self, text: str) -> bytes:
        request = urllib.request.Request(
            self._url,
            data=self.ssml(text).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._bearer()}",
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "raw-48khz-16bit-mono-pcm",
                "User-Agent": "harbor-voice",
            },
        )
        with self._opener(request, timeout=20) as response:
            return response.read()

    def _bearer(self) -> str:
        with self._lock:
            token = self._token
            if token is None or token.expires_on - 300 < self._clock():
                credential = self._credential or default_credential(self._tenant_id)
                self._credential = credential
                token = credential.get_token(_COGNITIVE_SCOPE)
                self._token = token
            return token.token
