from uuid import UUID

import pytest
from pydantic import ValidationError

from harbor_voice.domain import (
    ActionKind,
    AudioRecording,
    MessageResponse,
    ProposalResponse,
    parse_assistant_response,
)


def test_parse_message_response_catches_broken_response_dispatch() -> None:
    response = parse_assistant_response('{"kind":"message","message":"Hello"}')

    assert isinstance(response, MessageResponse)
    assert response.message == "Hello"


def test_parse_proposal_response_catches_lost_action_fields() -> None:
    response = parse_assistant_response(
        """{
          "kind": "proposal",
          "message": "I can open that.",
          "action": {
            "id": "3d8db2f0-c5ac-4d65-9b2e-508880d79d4b",
            "kind": "open_url",
            "target": "https://example.com",
            "summary": "Open Example"
          }
        }"""
    )

    assert isinstance(response, ProposalResponse)
    assert response.action.id == UUID("3d8db2f0-c5ac-4d65-9b2e-508880d79d4b")
    assert response.action.kind is ActionKind.OPEN_URL


def test_proposal_without_action_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_assistant_response('{"kind":"proposal","message":"Approve this"}')


def test_unknown_response_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_assistant_response('{"kind":"message","message":"Hello","execute":true}')


def test_recording_rejects_non_mono_audio() -> None:
    with pytest.raises(ValueError, match="mono"):
        AudioRecording(pcm=b"\x00\x00", sample_rate=16_000, channels=2)


def test_recording_rejects_odd_pcm_byte_count() -> None:
    with pytest.raises(ValueError, match="16-bit"):
        AudioRecording(pcm=b"\x00", sample_rate=16_000)

