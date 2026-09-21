"""What a device is sent of an answer, and in what pieces.

The conversation service streams the text of an answer as the model writes it. A speaker cannot
wait for the end (it would be silent for as long as the model takes) and cannot speak half a word,
so the text is cut into sentences as it arrives and each one is sent as soon as it is whole: the
first sound is heard after the first sentence, not after the answer.

What a device is sent depends on what it can render (``factotum.contracts.response``). A sentence is
spoken by a device with a speaker and shown by one with a screen (both, if it has both). The sources
an answer cited are a card for a screen and nothing for a speaker, which would only read them out.
Progress is a hint any device may ignore. A part a device cannot render is never sent to it.
"""

import re
from collections.abc import Sequence

from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card, CardItem
from factotum.contracts.conversation import (
    Citation,
    Cited,
    Delta,
    Failed,
    Finished,
    Phase,
    Status,
    TurnEvent,
)
from factotum.contracts.response import (
    CardPart,
    ResponsePart,
    Speech,
    StatusPart,
    StatusState,
    Text,
)

MIN_CHUNK_CHARS = 24
"""A sentence shorter than this is held back and said with the next one: "Yes." on its own is a
click, and the sentence after it is a moment away."""

MAX_CHUNK_CHARS = 280
"""A stretch of text longer than this with no end to a sentence in it is cut at a comma or a space:
a device must not wait for a full stop that a list, or a model, may not put there."""

_BOUNDARY = re.compile(r"[.!?…][\"')\]]*\s|\n")
"""The end of a sentence: a full stop and the space after it (so 3.5 is not cut), or a line."""

_CITATION = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")
_MARKER_START = re.compile(r"\s*(?:\[(?:\d+(?:\s*,\s*(?:\d+)?)*\s*)?)?")
"""What the start of a citation marker can look like before it is whole: the sentence it follows
is held until it is known whether it is one."""
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EMPHASIS = re.compile(r"(?<!\w)[*_]+|[*_]+(?!\w)|`+")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_HEADING = re.compile(r"^\s*#+\s*", re.MULTILINE)
_SPACES = re.compile(r"\s+")


class SentenceChunker:
    """Cuts text that arrives in pieces into sentences, whatever the pieces are.

    The cuts depend only on the text, not on how it was split when it arrived: the same answer gives
    the same sentences whether it came a word at a time or all at once.
    """

    def __init__(
        self, *, min_chars: int = MIN_CHUNK_CHARS, max_chars: int = MAX_CHUNK_CHARS
    ) -> None:
        self._min = min_chars
        self._max = max_chars
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        """The sentences that ``text`` completed."""
        self._buffer += text
        chunks: list[str] = []
        while (cut := self._next_cut(self._buffer)) is not None:
            chunk, self._buffer = self._buffer[:cut], self._buffer[cut:]
            if chunk.strip():
                chunks.append(chunk.strip())
        return chunks

    def flush(self) -> list[str]:
        """What is left when nothing more is coming."""
        rest, self._buffer = self._buffer.strip(), ""
        chunks: list[str] = []
        while len(rest) > self._max:
            cut = self._cut_long(rest)
            chunks.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        if rest:
            chunks.append(rest)
        return chunks

    def _next_cut(self, text: str) -> int | None:
        for match in _BOUNDARY.finditer(text):
            end = match.end()
            if end > self._max:
                break
            if len(text[:end].strip()) < self._min:
                continue
            # A model may put the marker of a citation after the full stop ("... seconds. [1]"):
            # it belongs to the sentence it follows, so the sentence waits until what comes next is
            # known not to be one.
            after = text[end:]
            if marker := _CITATION.match(after):
                return end + marker.end() if end + marker.end() <= self._max else end
            if _MARKER_START.fullmatch(after):
                return None
            return end
        if len(text) > self._max:
            return self._cut_long(text)
        return None

    def _cut_long(self, text: str) -> int:
        window = text[: self._max]
        for pattern in (r"[,;:]\s", r"\s"):
            ends = [match.end() for match in re.finditer(pattern, window)]
            if ends:
                return ends[-1]
        return self._max


def speakable(text: str) -> str:
    """What a voice should say of ``text``: the markers of a citation, a link's address and the
    marks of markdown are for the eyes."""
    text = _CITATION.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _LIST_MARKER.sub("", text)
    text = _EMPHASIS.sub("", text)
    return _SPACES.sub(" ", text).strip()


_STATES = {
    Phase.UNDERSTANDING: StatusState.THINKING,
    Phase.RECALLING: StatusState.THINKING,
    Phase.SEARCHING: StatusState.SEARCHING,
    Phase.ACTING: StatusState.ACTING,
}

_APOLOGIES = {
    "chat_not_configured": "I have no model to answer with yet.",
    "context_too_long": "That is too long for me to answer.",
    "conversation_unavailable": "I cannot reach my brain right now.",
}
_APOLOGY = "Sorry, I could not answer that."


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _where(citation: Citation) -> str | None:
    parts: list[str] = []
    if citation.page is not None:
        parts.append(f"page {citation.page}")
    if citation.headings:
        parts.append(" > ".join(citation.headings))
    return _clip(" · ".join(parts), 200) if parts else None


def sources_card(citations: Sequence[Citation]) -> Card:
    """The documents an answer drew on, for a screen."""
    return Card(
        title="Sources",
        items=tuple(
            CardItem(label=_clip(f"[{c.number}] {c.document_title}", 100), value=_where(c))
            for c in citations[:20]
        ),
    )


class TurnParts:
    """The parts of the answer to one utterance, for one device, as its events arrive."""

    def __init__(self, capabilities: frozenset[Capability]) -> None:
        self._speaks = Capability.AUDIO_OUT in capabilities
        self._shows = Capability.DISPLAY in capabilities
        self._chunker = SentenceChunker()
        self._state: StatusState | None = None

    def feed(self, event: TurnEvent) -> list[ResponsePart]:
        """What to send now, for one event of the turn."""
        if isinstance(event, Status):
            state = _STATES.get(event.phase)
            if state is None or state is self._state:
                return []
            self._state = state
            return [StatusPart(state=state)]
        if isinstance(event, Delta):
            return self._render(self._chunker.feed(event.text))
        if isinstance(event, Cited):
            parts = self.finish()
            if self._shows and event.citations:
                parts.append(CardPart(card=sources_card(event.citations)))
            return parts
        if isinstance(event, Finished):
            return self.finish()
        if isinstance(event, Failed):
            return self.failure(event.code)
        return []

    def finish(self) -> list[ResponsePart]:
        """What is left of the answer when nothing more is coming."""
        return self._render(self._chunker.flush())

    def failure(self, code: str) -> list[ResponsePart]:
        """The rest of what was written, and a word to say that it stops there."""
        return [*self.finish(), *self._render([_APOLOGIES.get(code, _APOLOGY)])]

    def _render(self, sentences: list[str]) -> list[ResponsePart]:
        parts: list[ResponsePart] = []
        for sentence in sentences:
            if self._speaks and (aloud := speakable(sentence)):
                parts.append(Speech(text=_clip(aloud, 2000)))
            if self._shows:
                parts.append(Text(text=sentence))
        return parts
