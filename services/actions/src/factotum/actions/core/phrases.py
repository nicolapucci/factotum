"""What the skills of the platform that act on a device say of what they did, in the language of the
person (ADR 0029).

A sentence is chosen by the code and not written by a model, as every sentence about an action is
(``factotum.actions.phrases``): the same each time, and checked in each language by a test. English
and Italian are here; another language is answered in English until somebody adds it.
"""

from enum import StrEnum

from factotum.actions.phrases import language_of


class CoreSay(StrEnum):
    VOLUME_AT = "volume_at"
    """The volume was set, or turned, and this is where it is."""

    MUTED = "muted"
    UNMUTED = "unmuted"

    SOUND_AT = "sound_at"
    """How it is, when the sound is on."""

    SOUND_MUTED = "sound_muted"
    """How it is, when the sound is muted."""


_CATALOG: dict[str, dict[CoreSay, str]] = {
    "en": {
        CoreSay.VOLUME_AT: "Volume at {level} percent.",
        CoreSay.MUTED: "Muted.",
        CoreSay.UNMUTED: "The sound is back.",
        CoreSay.SOUND_AT: "The volume is at {level} percent.",
        CoreSay.SOUND_MUTED: "The sound is muted. The volume is at {level} percent.",
    },
    "it": {
        CoreSay.VOLUME_AT: "Volume al {level} per cento.",
        CoreSay.MUTED: "Audio in muto.",
        CoreSay.UNMUTED: "Audio riattivato.",
        CoreSay.SOUND_AT: "Il volume è al {level} per cento.",
        CoreSay.SOUND_MUTED: "L'audio è in muto. Il volume è al {level} per cento.",
    },
}

LANGUAGES = frozenset(_CATALOG)


def core_say(language: str | None, key: CoreSay, /, **fields: str | int) -> str:
    """A sentence, in the language if it is known and in English if it is not."""
    return _CATALOG[language_of(language)][key].format_map(fields)
