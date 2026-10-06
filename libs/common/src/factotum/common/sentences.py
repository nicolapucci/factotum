"""Text cut into what a voice says one at a time, and the text a voice should say.

A speaker cannot wait for the end of an answer (it would be silent for as long as the model
takes) and cannot speak half a word, so a text is cut into sentences and each is said as soon as it
is whole: the first sound is heard after the first sentence. The edge does it for the answer to a
person as it is written, and for a text that a skill asks a device to say (``speak``); a skill of
the platform does it for an announcement. So what a sentence is lives here.
"""

import re

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


def sentences_of(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """What a voice is to say of a whole text, a sentence at a time and in order: cut as an answer
    is cut, and each with what is for the eyes taken out of it. Nothing for a text that has nothing
    to say (it is marks, or it is empty)."""
    chunker = SentenceChunker(max_chars=max_chars)
    chunks = [*chunker.feed(text), *chunker.flush()]
    return [aloud for chunk in chunks if (aloud := speakable(chunk))]
