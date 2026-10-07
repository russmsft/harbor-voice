"""In-memory rolling meeting transcript and addressing rules."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from time import monotonic

ME = "Me"
PARTICIPANT = "Participant"

_QUESTION_START = re.compile(
    r"^(who|what|when|where|why|how|which|can|could|would|will|should|is|are|do|does|"
    r"did|have|has|any|anyone)\b",
    re.IGNORECASE,
)
_STOP_PHRASES = frozenset(
    {
        "stop",
        "stop talking",
        "be quiet",
        "quiet",
        "that's enough",
        "thats enough",
        "enough",
        "cancel",
        "shush",
        "pause",
        "hold on",
    }
)
_FILLER = re.compile(r"\b(ok|okay|hey|thanks|thank you|please|you can)\b")


@dataclass(frozen=True, slots=True)
class Utterance:
    speaker: str
    text: str
    at: float

    @property
    def is_question(self) -> bool:
        stripped = self.text.strip()
        return stripped.endswith("?") or bool(_QUESTION_START.match(stripped))


def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.casefold()).split())


def _wake_pattern(wake_words: Iterable[str]) -> re.Pattern[str] | None:
    words = [re.escape(_normalise(word)) for word in wake_words if _normalise(word)]
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(words) + r")\b")


def addresses_agent(text: str, wake_words: Iterable[str]) -> bool:
    """True when the utterance names the agent."""
    pattern = _wake_pattern(wake_words)
    return pattern is not None and pattern.search(_normalise(text)) is not None


def is_stop_command(text: str, wake_words: Iterable[str]) -> bool:
    """True for short utterances such as 'Scout, stop' or 'OK Scout, that's enough'."""
    pattern = _wake_pattern(wake_words)
    if pattern is None:
        return False
    normalised = _normalise(text)
    match = pattern.search(normalised)
    if match is None:
        return False
    remainder = normalised[: match.start()] + " " + normalised[match.end() :]
    remainder = " ".join(_FILLER.sub(" ", remainder).split())
    return remainder in _STOP_PHRASES


class MeetingTranscript:
    """Bounded, memory-only transcript. Nothing is persisted."""

    def __init__(
        self,
        *,
        max_age_seconds: float = 900.0,
        max_items: int = 400,
        clock=monotonic,
    ) -> None:
        self._max_age = max_age_seconds
        self._items: deque[Utterance] = deque(maxlen=max_items)
        self._clock = clock
        self._origin = clock()

    def __len__(self) -> int:
        return len(self._items)

    def add(self, speaker: str, text: str) -> Utterance | None:
        cleaned = " ".join(text.split())
        if not cleaned:
            return None
        item = Utterance(speaker, cleaned, self._clock())
        self._items.append(item)
        self._prune()
        return item

    def clear(self) -> None:
        self._items.clear()
        self._origin = self._clock()

    def items(self) -> list[Utterance]:
        self._prune()
        return list(self._items)

    def latest_question(
        self,
        *,
        within_seconds: float = 180.0,
        speakers: tuple[str, ...] = (PARTICIPANT,),
    ) -> Utterance | None:
        cutoff = self._clock() - within_seconds
        recent = [item for item in self.items() if item.at >= cutoff and item.speaker in speakers]
        for item in reversed(recent):
            if item.is_question:
                return item
        return recent[-1] if recent else None

    def render(self, *, max_chars: int = 6_000) -> str:
        lines: list[str] = []
        used = 0
        for item in reversed(self.items()):
            offset = max(0, int(item.at - self._origin))
            line = f"[{offset // 60:02d}:{offset % 60:02d}] {item.speaker}: {item.text}"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(reversed(lines))

    def _prune(self) -> None:
        cutoff = self._clock() - self._max_age
        while self._items and self._items[0].at < cutoff:
            self._items.popleft()
