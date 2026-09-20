"""Which language a document is in, so that its text is searched the way that language is written.

Stemming and stop words differ from language to language, and Postgres knows a few dozen. The
language of a document is detected once, when it is ingested, from a sample of its prose, with a
small offline detector (no model server, no LLM).

This is about the *text* side of a search. Finding a passage in another language than the question
is the job of a multilingual embedding model, which puts both in the same space; detecting the
language cannot do it, and the search does not need the language of the query at all: it searches
in the language of each document it looks through.
"""

from collections import defaultdict

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

from factotum.knowledge.document import Block

DetectorFactory.seed = 0  # the same text must always give the same answer

FTS_CONFIGS: dict[str, str] = {
    "ar": "arabic",
    "ca": "catalan",
    "da": "danish",
    "de": "german",
    "el": "greek",
    "en": "english",
    "es": "spanish",
    "fi": "finnish",
    "fr": "french",
    "hi": "hindi",
    "hu": "hungarian",
    "id": "indonesian",
    "it": "italian",
    "ne": "nepali",
    "nl": "dutch",
    "no": "norwegian",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "sv": "swedish",
    "ta": "tamil",
    "tr": "turkish",
}
"""The languages Postgres has a text search configuration for."""

_WINDOWS = 8
_WINDOW_CHARS = 1500
_MIN_WORDS_IN_PROSE = 8
_MIN_CONFIDENCE = 0.6


def fts_config(language: str | None) -> str:
    """The Postgres text search configuration for a language; ``simple`` when there is none."""
    return FTS_CONFIGS.get(language or "", "simple")


def normalize_language(code: str) -> str | None:
    """A language code as this service keeps it: two or three lower-case letters, no region."""
    base = code.strip().lower().replace("_", "-").split("-")[0]
    return base if 2 <= len(base) <= 3 and base.isalpha() else None


def _is_prose(block: Block) -> bool:
    if block.kind not in {"text", "heading"}:
        return False
    words = block.text.split()
    if len(words) < _MIN_WORDS_IN_PROSE:
        return False
    letters = sum(char.isalpha() or char.isspace() for char in block.text)
    return letters / len(block.text) >= 0.85


def _windows(blocks: list[Block]) -> list[str]:
    """Up to a few windows of prose spread over the whole document: a book has an introduction, a
    body and code listings, and the language is the one of the prose."""
    prose = [block.text for block in blocks if _is_prose(block)]
    if not prose:
        return []
    step = max(1, len(prose) // _WINDOWS)
    starts = range(0, len(prose), step)
    windows: list[str] = []
    for start in starts:
        window = ""
        for text in prose[start:]:
            window += text + "\n"
            if len(window) >= _WINDOW_CHARS:
                break
        windows.append(window)
        if len(windows) == _WINDOWS:
            break
    return windows


def detect_language(blocks: list[Block]) -> str | None:
    """The language the prose of a document is in, or None when it is not clear."""
    windows = _windows(blocks)
    if not windows:
        return None
    scores: dict[str, float] = defaultdict(float)
    for window in windows:
        try:
            candidates = detect_langs(window)
        except LangDetectException:
            continue
        for candidate in candidates:
            language = normalize_language(candidate.lang)
            if language is not None:
                scores[language] += candidate.prob * len(window)
    if not scores:
        return None
    best, total = max(scores.items(), key=lambda item: item[1]), sum(scores.values())
    return best[0] if best[1] / total >= _MIN_CONFIDENCE else None
