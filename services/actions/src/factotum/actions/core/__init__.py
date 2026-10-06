"""The skills that the platform provides, in code (ADR 0026)."""

from factotum.actions.core.announce import Announce
from factotum.actions.core.skills import CoreResult, CoreSkill, CoreSkills, SpeechMaker


def platform_skills(speech: SpeechMaker) -> CoreSkills:
    """The skills that the platform provides, with the voice that they ask for."""
    return CoreSkills(Announce(speech))


__all__ = [
    "Announce",
    "CoreResult",
    "CoreSkill",
    "CoreSkills",
    "SpeechMaker",
    "platform_skills",
]
