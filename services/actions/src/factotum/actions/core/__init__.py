"""The skills that the platform provides, in code (ADR 0026)."""

from factotum.actions.core.alarms import (
    Alarm,
    AlarmCancel,
    AlarmChange,
    AlarmPostpone,
    AlarmStatus,
)
from factotum.actions.core.announce import Announce
from factotum.actions.core.skills import CoreResult, CoreSkill, CoreSkills, SpeechMaker
from factotum.actions.core.stop import Stop
from factotum.actions.core.timers import Timer, TimerCancel, TimerStatus
from factotum.actions.core.volume import Mute, Volume, VolumeChange, VolumeStatus


def platform_skills(speech: SpeechMaker) -> CoreSkills:
    """The skills that the platform provides, with the voice that they ask for."""
    return CoreSkills(
        Announce(speech),
        Volume(),
        VolumeChange(),
        Mute(),
        VolumeStatus(),
        Timer(speech),
        TimerCancel(),
        TimerStatus(),
        Alarm(speech),
        AlarmCancel(),
        AlarmPostpone(),
        AlarmChange(),
        AlarmStatus(),
        Stop(),
    )


__all__ = [
    "Alarm",
    "AlarmCancel",
    "AlarmChange",
    "AlarmPostpone",
    "AlarmStatus",
    "Announce",
    "CoreResult",
    "CoreSkill",
    "CoreSkills",
    "Mute",
    "SpeechMaker",
    "Stop",
    "Timer",
    "TimerCancel",
    "TimerStatus",
    "Volume",
    "VolumeChange",
    "VolumeStatus",
    "platform_skills",
]
