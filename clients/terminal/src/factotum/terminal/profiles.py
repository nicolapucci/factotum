"""The kinds of device the terminal client can be.

A profile is what a real device would be: what it can do, and the screen it has if it has one. The
terminal behaves as the device would (it shows what its hardware could render, and nothing else), so
that the edge can be looked at from the side of a speaker, of a screen without a microphone, of a
phone. Each profile is a device of ``config/devices.example.yaml``, and a test says so: the hardware
id and the credential are those of the example, which is development only.
"""

from dataclasses import dataclass

from factotum.contracts.capabilities import Capability, DisplayInfo


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    description: str
    capabilities: frozenset[Capability]
    display: DisplayInfo | None
    hardware_id: str
    token: str
    """The credential of the device in ``config/devices.example.yaml``. It is a placeholder for
    development: the hub issues real ones."""


PROFILES: dict[str, Profile] = {
    profile.name: profile
    for profile in (
        Profile(
            name="speaker",
            description="a speaker with a microphone and a light: it speaks, and shows nothing",
            capabilities=frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT, Capability.LED}),
            display=None,
            hardware_id="kitchen-01",
            token="kitchen-dev-token",  # noqa: S106 - the placeholder of the example file
        ),
        Profile(
            name="smart-display",
            description="a screen with a speaker and a microphone: it speaks and shows the answer",
            capabilities=frozenset(
                {Capability.AUDIO_IN, Capability.AUDIO_OUT, Capability.DISPLAY, Capability.LED}
            ),
            display=DisplayInfo(width=800, height=480, touch=True),
            hardware_id="living-room-01",
            token="living-room-dev-token",  # noqa: S106
        ),
        Profile(
            name="screen",
            description="a screen with no microphone or speaker: it cannot start a conversation",
            capabilities=frozenset({Capability.DISPLAY}),
            display=DisplayInfo(width=1024, height=600),
            hardware_id="hallway-01",
            token="hallway-dev-token",  # noqa: S106
        ),
        Profile(
            name="player",
            description="a streamer with no microphone: it plays what it is told to",
            capabilities=frozenset({Capability.AUDIO_OUT, Capability.MEDIA_PLAYER}),
            display=None,
            hardware_id="study-01",
            token="study-dev-token",  # noqa: S106
        ),
        Profile(
            name="phone",
            description="a personal phone: it acts for its owner, speaks and shows the answer",
            capabilities=frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT, Capability.DISPLAY}),
            display=DisplayInfo(width=390, height=844, touch=True),
            hardware_id="phone-01",
            token="phone-dev-token",  # noqa: S106
        ),
    )
}

DEFAULT_PROFILE = "speaker"


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"there is no profile {name!r}: the profiles are {', '.join(sorted(PROFILES))}"
        ) from None
