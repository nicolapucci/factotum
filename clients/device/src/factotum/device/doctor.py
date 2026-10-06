"""``factotum-device doctor``: what is wrong, said in a line each, before a person has to guess.

A device that does not answer has many reasons to be silent (the credential, the address of the
edge, a microphone that Windows does not let a program hear, a speaker that does not take the rate
of the voice), and from outside they look the same. The doctor tries each of them by itself and
says which, and what to do about it. It makes no sound and hears nothing unless it is asked to
(``--listen``, ``--play``), and it connects to the edge *as the device*: a device that is running
with the same hardware id is replaced by it, which is the rule of the edge (the last connection of a
device wins), so a device is stopped before it is looked at.
"""

import asyncio
import io
import math
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from factotum.client_audio import AudioBackend, AudioUnavailableError, Microphone, Speaker
from factotum.client_core import CouldNotConnect, DeviceClient, Refused, WebSocketConnection
from factotum.client_core.connection import Connection
from factotum.client_core.ports import CommandRouter
from factotum.device.config import Config, Mode
from factotum.device.hands import DeviceHands
from factotum.device.render import DeviceRenderer

SPEAKER_RATE = 22_050
"""What the voice of the Italian Piper is made at, and so what a speaker has to take."""

TONE_HZ = 440
TONE_SECONDS = 0.4
LISTEN_SECONDS = 2.0
BLOCK_WAIT_S = 2.0
"""How long a microphone may take to hand over a block before it is called dead."""

QUIET = 20.0
"""A mean level, of 32768, under which a microphone has heard nothing at all."""


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    advice: str | None = None
    """What to do about it, when it failed."""


def tone(rate: int = SPEAKER_RATE, seconds: float = TONE_SECONDS, hz: int = TONE_HZ) -> bytes:
    """A sine wave of 16-bit samples, quiet enough not to startle anybody, with a short fade at each
    end so that it does not click."""
    count = int(rate * seconds)
    fade = max(1, int(rate * 0.02))
    samples = []
    for index in range(count):
        envelope = min(1.0, index / fade, (count - index) / fade)
        samples.append(int(6000 * envelope * math.sin(2 * math.pi * hz * index / rate)))
    return struct.pack(f"<{count}h", *samples)


async def check_devices(config: Config, backend: AudioBackend) -> Check:
    try:
        listed = await asyncio.to_thread(backend.devices)
    except AudioUnavailableError as error:
        return Check(
            "sound devices",
            False,
            str(error),
            "on Linux: apt install libportaudio2 (and check that this user may use the sound card)",
        )
    default_in, default_out = backend.default_input(), backend.default_output()
    lines = []
    for device in listed:
        marks = []
        if device.index == default_in:
            marks.append("default microphone")
        if device.index == default_out:
            marks.append("default speaker")
        kind = "/".join(
            kind for kind, count in (("in", device.inputs), ("out", device.outputs)) if count
        )
        suffix = f"  <- {', '.join(marks)}" if marks else ""
        lines.append(f"{device.index}: {device.name} [{device.api}; {kind or '-'}]{suffix}")
    wanted = (config.can_hear and not any(d.inputs for d in listed)) or (
        config.can_speak and not any(d.outputs for d in listed)
    )
    if wanted:
        return Check(
            "sound devices",
            False,
            "; ".join(lines) or "none",
            "a device with a microphone or a speaker needs one that the system lists",
        )
    return Check("sound devices", True, "\n".join(lines))


def check_microphone(config: Config, backend: AudioBackend) -> Check:
    chosen = config.input_device
    try:
        backend.check_input(chosen, Microphone(backend).sample_rate)
    except AudioUnavailableError as error:
        return Check(
            "microphone",
            False,
            str(error),
            "choose another with audio.input_device (a number, as listed, is the surest: the same "
            "hardware is listed by each host API, and a part of a name that fits several is "
            "refused); on Windows, Settings > Privacy > Microphone must let desktop apps hear",
        )
    return Check("microphone", True, f"{chosen if chosen is not None else 'the default'} hears")


def check_speaker(config: Config, backend: AudioBackend) -> Check:
    chosen = config.output_device
    try:
        backend.check_output(chosen, SPEAKER_RATE)
    except AudioUnavailableError as error:
        return Check(
            "speaker",
            False,
            str(error),
            "choose another with audio.output_device (a number, as listed, is the surest: the same "
            "hardware is listed by each host API, and a part of a name that fits several is "
            "refused)",
        )
    return Check("speaker", True, f"{chosen if chosen is not None else 'the default'} plays")


ADVICE = {
    "auth_failed": (
        "the edge does not know this hardware id with this credential: check device.hardware_id "
        "and the credential against the hub (or the file of devices of the edge)"
    ),
    "unsupported_protocol": "the edge speaks another version of the protocol: update this device",
    "connection_lost": "the edge closed the connection before it said it was ready",
}


async def check_edge(
    config: Config,
    speaker: Speaker | None,
    opening: Callable[[str], Awaitable[Connection]] = WebSocketConnection.open,
) -> Check:
    from factotum.device.app import identity_of  # not at the top: app imports this package's hands

    try:
        connection = await opening(config.edge.url)
    except CouldNotConnect as error:
        return Check(
            "edge",
            False,
            str(error),
            f"is the edge up, and is {config.edge.url} where it is? "
            "(docker compose ps; the edge listens on port 8084)",
        )

    learned = io.StringIO()  # what a device that is only being looked at says of itself
    renderer = DeviceRenderer(learned)
    client = DeviceClient(
        connection,
        identity_of(config),
        renderer,
        CommandRouter(DeviceHands(config.device.capabilities, speaker, renderer)),
        voice_output=speaker,
        voice_sink=speaker,
    )
    try:
        ready = await client.connect()
    except Refused as refused:
        await connection.close()
        return Check(
            "edge",
            False,
            f"{config.edge.url} refused this device: {refused}",
            ADVICE.get(refused.code, "see what the edge says: it logs why it sends a device away"),
        )
    try:
        features = ", ".join(sorted(f.value for f in ready.features)) or "none"
        detail = f"connected as {ready.name}; the edge will use: {features}"
        if config.can_speak and "voice_stream" not in features:
            detail += " (it will send a clip for each sentence, and not a stream)"
        bugs = [line[9:] for line in learned.getvalue().splitlines() if line.startswith("[BUG]")]
        if bugs:
            return Check("edge", False, f"{detail}; {'; '.join(bugs)}", "this is a bug of the edge")
        if ready.unregistered:
            ignored = ", ".join(sorted(c.value for c in ready.unregistered))
            return Check(
                "edge",
                False,
                f"{detail}; it is not registered for {ignored}, and will not use it",
                "register the device for it in the hub (or the file of devices), or declare "
                "less in device.capabilities",
            )
        return Check("edge", True, detail)
    finally:
        await client.close()


async def check_listening(config: Config, backend: AudioBackend) -> Check:
    """Hear for two seconds and say how loud it was: a microphone that hears nothing at all is one
    that is muted, or is not the one that was meant."""
    import numpy as np

    microphone = Microphone(backend, device=config.input_device)
    levels: list[float] = []
    try:
        async with microphone.opened() as blocks:
            wanted = int(LISTEN_SECONDS * microphone.sample_rate / microphone.block_size)
            for _ in range(wanted):
                block = await asyncio.wait_for(blocks.get(), BLOCK_WAIT_S)
                levels.append(float(np.abs(block).mean()))
    except (AudioUnavailableError, TimeoutError) as error:
        return Check("listening", False, str(error) or "nothing came from the microphone")
    mean, loudest = sum(levels) / len(levels), max(levels)
    if mean < QUIET and loudest < QUIET:
        return Check(
            "listening",
            False,
            f"heard nothing in {LISTEN_SECONDS:.0f} s (mean level {mean:.0f})",
            "is the microphone muted, or is it not the one that was meant?",
        )
    return Check("listening", True, f"mean level {mean:.0f}, loudest {loudest:.0f}")


async def check_playing(config: Config, speaker: Speaker) -> Check:
    """Play a tone, quietly, for a moment: a speaker that takes it but that nobody hears is
    something only a person can tell."""
    from factotum.contracts.audio import wav_of

    try:
        await speaker.play(wav_of(tone(), SPEAKER_RATE))
    except AudioUnavailableError as error:
        return Check("playing", False, str(error))
    return Check("playing", True, f"a tone of {TONE_HZ} Hz was played: did you hear it?")


async def examine(
    config: Config,
    backend: AudioBackend,
    *,
    listen: bool = False,
    play: bool = False,
    edge: bool = True,
    opening: Callable[[str], Awaitable[Connection]] = WebSocketConnection.open,
) -> list[Check]:
    """Every check that applies to this device, in the order a person would try them."""
    speaker = Speaker(backend, device=config.output_device, volume=config.audio.volume)
    microphone: Check | None = None
    checks = [
        Check(
            "settings",
            True,
            f"{config.device.hardware_id} on {config.edge.url}; "
            f"{', '.join(sorted(c.value for c in config.device.capabilities))}"
            + (f"; {config.listening.mode.value}" if config.can_hear else "")
            + (
                f" ({config.listening.wake_word!r})"
                if config.can_hear and config.listening.mode is Mode.WAKE_WORD
                else ""
            ),
        ),
        await check_devices(config, backend),
    ]
    if config.can_hear:
        microphone = check_microphone(config, backend)
        checks.append(microphone)
    if config.can_speak:
        checks.append(check_speaker(config, backend))
    if edge:
        checks.append(await check_edge(config, speaker if config.can_speak else None, opening))
    if listen and microphone is not None and microphone.ok:
        checks.append(await check_listening(config, backend))
    if play and config.can_speak:
        checks.append(await check_playing(config, speaker))
    return checks


def report_of(checks: list[Check]) -> str:
    """The checks as a person reads them: ``ok`` or ``FAIL``, the name and what was found, and what
    to do about what failed."""
    lines = []
    for check in checks:
        first, *rest = check.detail.splitlines() or [""]
        lines.append(f"{'ok  ' if check.ok else 'FAIL'}  {check.name:<14}{first}")
        lines.extend(f"      {'':<14}{line}" for line in rest)
        if check.advice and not check.ok:
            lines.append(f"      {'':<14}-> {check.advice}")
    return "\n".join(lines)
