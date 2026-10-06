"""The device, put together: the core of every client, the sound hardware of this one, and what the
settings say it is.

What a client does of the protocol is the core's (``factotum.client_core``); what it does with a
sound card is the library's (``factotum.client_audio``); this is the part that is a device and
nothing else: it is who it was paired as, it asks for what it has (a microphone, a speaker), it
listens the way it was told to, and it stays connected, patiently, for as long as nobody stops it.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from factotum.client_audio import (
    AudioBackend,
    EnterKey,
    Microphone,
    PushToTalk,
    Speaker,
    WakeWord,
)
from factotum.client_audio.inputs import Presses
from factotum.client_core import (
    Backoff,
    CommandRouter,
    Connection,
    ConnectionLost,
    DeviceClient,
    DeviceIdentity,
    Outcome,
    Renderer,
    VoiceInput,
    VoiceLoop,
    WebSocketConnection,
    stay_connected,
)
from factotum.device.config import Config, ConfigError, Mode
from factotum.device.hands import DeviceHands


@dataclass(frozen=True, slots=True)
class Hardware:
    """What the device is made of, as far as the sound goes."""

    speaker: Speaker | None
    """What it says an answer with: None for a device that has no speaker."""

    voice_input: VoiceInput | None
    """What it hears a person by: None for a device that cannot start a conversation."""


OpenConnection = Callable[[str], Awaitable[Connection]]


def identity_of(config: Config) -> DeviceIdentity:
    """Who the device is, as it says it when it connects."""
    return DeviceIdentity(
        hardware_id=config.device.hardware_id,
        token=config.token,
        capabilities=config.device.capabilities,
    )


def build_hardware(
    config: Config,
    backend: AudioBackend,
    renderer: Renderer,
    *,
    presses: Presses | None = None,
) -> Hardware:
    """The speaker and the way of hearing that the settings describe, on the sound card that
    ``backend`` is. ``presses`` stands in for the keyboard, for a test."""
    speaker = (
        Speaker(backend, device=config.output_device, volume=config.audio.volume)
        if config.can_speak
        else None
    )
    if not config.can_hear:
        return Hardware(speaker, None)
    listening = config.listening

    def on_listening() -> None:
        renderer.notice("listening")

    voice_input: VoiceInput
    if listening.mode is Mode.WAKE_WORD:
        if listening.access_key is None:
            raise ConfigError("listening.mode wake_word needs an access key")
        voice_input = WakeWord(
            backend,
            access_key=listening.access_key.get_secret_value(),
            wake_word=listening.wake_word,
            device=config.input_device,
            silence_seconds=listening.silence_seconds,
            silence_level=listening.silence_level,
            max_seconds=listening.max_seconds,
            on_listening=on_listening,
        )
    else:
        voice_input = PushToTalk(
            presses if presses is not None else EnterKey(),
            Microphone(backend, device=config.input_device),
            silence_seconds=listening.silence_seconds,
            silence_level=listening.silence_level,
            max_seconds=listening.max_seconds,
            on_listening=on_listening,
        )
    return Hardware(speaker, voice_input)


async def open_client(
    config: Config,
    hardware: Hardware,
    renderer: Renderer,
    opening: OpenConnection = WebSocketConnection.open,
) -> DeviceClient:
    """A client for the device, connected to the edge and told it is ready. Raises
    ``CouldNotConnect`` when the edge cannot be reached and ``Refused`` when it does not accept the
    device."""
    connection = await opening(config.edge.url)
    hands = DeviceHands(config.device.capabilities, hardware.speaker, renderer)
    client = DeviceClient(
        connection,
        identity_of(config),
        renderer,
        CommandRouter(hands),
        voice_output=hardware.speaker,
        voice_sink=hardware.speaker,
    )
    try:
        await client.connect()
    except BaseException:
        await connection.close()
        raise
    return client


async def run_device(
    config: Config,
    hardware: Hardware,
    renderer: Renderer,
    *,
    opening: OpenConnection = WebSocketConnection.open,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    chance: Callable[[], float] = random.random,
) -> Outcome:
    """Be the device until the person leaves, or the edge makes clear that it is no use asking
    again. Cancelling this is how it is stopped: what is playing is cut and the edge is said
    goodbye."""
    loop = (
        VoiceLoop(
            hardware.voice_input, listen_while_answering=config.listening.listen_while_answering
        )
        if hardware.voice_input is not None
        else None
    )

    async def connect() -> DeviceClient:
        return await open_client(config, hardware, renderer, opening)

    async def serve(client: DeviceClient) -> ConnectionLost | None:
        if loop is not None:
            return await loop.serve(client)
        try:  # nothing here starts a conversation: it does what the edge sends, and that is all
            return await client.run()
        finally:
            await client.close()

    if not config.can_hear:
        renderer.notice("this device has no microphone: it is spoken through")
    return await stay_connected(
        connect,
        serve,
        renderer,
        backoff=Backoff(first_s=config.reconnect.first_s, max_s=config.reconnect.max_s),
        sleep=sleep,
        chance=chance,
    )
