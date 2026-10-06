"""What a device is sent of an answer, and in what pieces.

The conversation service streams the text of an answer as the model writes it. A speaker cannot
wait for the end (it would be silent for as long as the model takes) and cannot speak half a word,
so the text is cut into sentences as it arrives (``factotum.common.sentences``) and each one is sent
as soon as it is whole: the first sound is heard after the first sentence, not after the answer.

What a device is sent depends on what it can render (``factotum.contracts.response``). A sentence is
spoken by a device with a speaker and shown by one with a screen (both, if it has both). The sources
an answer cited are a card for a screen and nothing for a speaker, which would only read them out.
Progress is a hint any device may ignore. A part a device cannot render is never sent to it.
"""

from collections.abc import Sequence

from factotum.common.sentences import SentenceChunker, speakable
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
