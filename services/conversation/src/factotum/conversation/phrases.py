"""What the assistant says when it has nothing to answer from (ADR 0028), in the language of the
person.

A model answers nothing by itself: with no skill that fits a message, no passage of the documents
that is relevant and nothing said before that it refers to, there is nothing for a model to work
on, and it is not asked. What is said is one of a few sentences that the code chooses, so that it
is the same every time and a test can check it in each language. They say which of the two was
missing and, when one could not be reached, that it could not: a person fixes a skill that is not
there in another way than a service that is away.

English and Italian are here, as in the actions service; another language is answered in English
until somebody adds it, and a test says that no sentence is missing from any of them.
"""

from enum import StrEnum

DEFAULT_LANGUAGE = "en"


class Say(StrEnum):
    NO_SKILL_NO_DOCUMENTS = "no_skill_no_documents"
    """No skill fits, and nothing in the documents is relevant."""

    NO_SKILL_DOCUMENTS_AWAY = "no_skill_documents_away"
    """No skill fits, and the documents could not be searched."""

    SKILLS_AWAY_NO_DOCUMENTS = "skills_away_no_documents"
    """Nobody could say whether a skill fits, and nothing in the documents is relevant."""

    SKILLS_AWAY_DOCUMENTS_AWAY = "skills_away_documents_away"
    """Neither the skills nor the documents could be reached."""

    NOTHING_REMEMBERED = "nothing_remembered"
    """The message refers to what was said before, and none of it is to be had."""


_CATALOG: dict[str, dict[Say, str]] = {
    "en": {
        Say.NO_SKILL_NO_DOCUMENTS: "I found no skill that fits, and no relevant document.",
        Say.NO_SKILL_DOCUMENTS_AWAY: (
            "I found no skill that fits, and I cannot reach your documents right now."
        ),
        Say.SKILLS_AWAY_NO_DOCUMENTS: (
            "I cannot reach the skills right now, and I found no relevant document."
        ),
        Say.SKILLS_AWAY_DOCUMENTS_AWAY: ("I cannot reach the skills or your documents right now."),
        Say.NOTHING_REMEMBERED: "I do not remember what was said before.",
    },
    "it": {
        Say.NO_SKILL_NO_DOCUMENTS: "Non ho trovato una skill adatta, né documenti pertinenti.",
        Say.NO_SKILL_DOCUMENTS_AWAY: (
            "Non ho trovato una skill adatta, e non riesco a consultare i tuoi documenti "
            "in questo momento."
        ),
        Say.SKILLS_AWAY_NO_DOCUMENTS: (
            "Non riesco a consultare le skill in questo momento, e non ho trovato documenti "
            "pertinenti."
        ),
        Say.SKILLS_AWAY_DOCUMENTS_AWAY: (
            "Non riesco a consultare né le skill né i tuoi documenti in questo momento."
        ),
        Say.NOTHING_REMEMBERED: "Non ricordo cosa è stato detto prima.",
    },
}

LANGUAGES = frozenset(_CATALOG)


def language_of(code: str | None, default: str = DEFAULT_LANGUAGE) -> str:
    """The language to answer in: the one that was read, if there are words for it, and the one
    the deployment answers in otherwise (English, when that has no words either)."""
    if code is not None and code in _CATALOG:
        return code
    return default if default in _CATALOG else DEFAULT_LANGUAGE


def say(language: str | None, key: Say, /, default: str = DEFAULT_LANGUAGE) -> str:
    return _CATALOG[language_of(language, default)][key]


def nothing_to_answer_from(
    language: str | None,
    *,
    skills_away: bool,
    documents_away: bool,
    default: str = DEFAULT_LANGUAGE,
) -> str:
    """The sentence for a message that no skill, document or memory can answer: which was missing,
    and which could not be reached."""
    if skills_away:
        key = Say.SKILLS_AWAY_DOCUMENTS_AWAY if documents_away else Say.SKILLS_AWAY_NO_DOCUMENTS
    else:
        key = Say.NO_SKILL_DOCUMENTS_AWAY if documents_away else Say.NO_SKILL_NO_DOCUMENTS
    return say(language, key, default)
