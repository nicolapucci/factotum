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
    WakeWordEngine,
    WakeWordError,
    WakeWordModels,
    wake_word_engine,
)
from factotum.client_audio.inputs import Presses
from factotum.client_core import (
    Backoff,
    Clock,
    CommandRouter,
    Connection,
    ConnectionLost,
    DeviceClient,
    DeviceIdentity,
    Outcome,
    Renderer,
    Timekeeper,
    VoiceInput,
    VoiceLoop,
    WebSocketConnection,
    stay_connected,
)
from factotum.device.config import Config, ConfigError, ListeningSettings, Mode
from factotum.device.hands import DeviceHands


@dataclass(frozen=True, slots=True)
class Hardware:
    """What the device is made of, as far as the sound goes."""

    speaker: Speaker | None
    """What it says an answer with: None for a device that has no speaker."""

    voice_input: VoiceInput | None
    """What it hears a person by: None for a device that cannot start a conversation."""


OpenConnection = Callable[[str], Awaitable[Connection]]

EngineFactory = Callable[[ListeningSettings], Callable[[], WakeWordEngine]]
"""What makes the engine that watches for the word, from the settings that say which and for what:
``engine_of``, or another for a test."""


def identity_of(config: Config) -> DeviceIdentity:
    """Who the device is, as it says it when it connects."""
    return DeviceIdentity(
        hardware_id=config.device.hardware_id,
        token=config.token,
        capabilities=config.device.capabilities,
    )


def engine_of(listening: ListeningSettings) -> Callable[[], WakeWordEngine]:
    """What makes the engine for the word that the settings name, when it is called (the models are
    loaded, or the key is validated, then and not before)."""
    key = listening.access_key.get_secret_value() if listening.access_key is not None else None
    try:
        return wake_word_engine(
            listening.engine,
            listening.word,
            access_key=key,
            models=WakeWordModels(listening.models_dir),
            threshold=listening.threshold,
            patience=listening.patience,
        )
    except WakeWordError as error:
        raise ConfigError(str(error)) from None


def build_hardware(
    config: Config,
    backend: AudioBackend,
    renderer: Renderer,
    *,
    presses: Presses | None = None,
    engine: EngineFactory = engine_of,
) -> Hardware:
    """The speaker and the way of hearing that the settings describe, on the sound card that
    ``backend`` is. ``presses`` stands in for the keyboard, and ``engine`` for what watches for the
    word, for a test."""
    speaker = (
        Speaker(
            backend,
            device=config.output_device,
            volume=config.audio.volume,
            step=config.audio.volume_step,
        )
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
        voice_input = WakeWord(
            backend,
            engine=engine(listening),
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
    timekeeper: Timekeeper | None = None,
) -> DeviceClient:
    """A client for the device, connected to the edge and told it is ready. Raises
    ``CouldNotConnect`` when the edge cannot be reached and ``Refused`` when it does not accept the
    device. The timers and the alarms are the device's and not a connection's: ``timekeeper`` is the
    one that outlives it (a client with none keeps its own, which goes with it)."""
    connection = await opening(config.edge.url)
    hands = DeviceHands(config.device.capabilities, hardware.speaker, renderer)
    client = DeviceClient(
        connection,
        identity_of(config),
        renderer,
        CommandRouter(hands),
        voice_output=hardware.speaker,
        voice_sink=hardware.speaker,
        timekeeper=timekeeper,
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
    clock: Clock | None = None,
) -> Outcome:
    """Be the device until the person leaves, or the edge makes clear that it is no use asking
    again. Cancelling this is how it is stopped: what is playing is cut and the edge is said
    goodbye.

    The timers and the alarms of the device (ADR 0029) are kept here, once, on ``clock`` (the
    machine's, unless a test gives another): they go on when the connection is lost, ring on the
    speaker when nobody is connected, and are lost when this ends."""
    loop = (
        VoiceLoop(
            hardware.voice_input, listen_while_answering=config.listening.listen_while_answering
        )
        if hardware.voice_input is not None
        else None
    )

    keeper = (
        Timekeeper(
            renderer,
            clock=clock,
            output=hardware.speaker,
            snooze_s=config.timekeeping.snooze_minutes * 60,
        )
        if hardware.speaker is not None
        else None
    )

    async def connect() -> DeviceClient:
        return await open_client(config, hardware, renderer, opening, keeper)

    async def serve(client: DeviceClient) -> ConnectionLost | None:
        if loop is not None:
            return await loop.serve(client)
        try:  # nothing here starts a conversation: it does what the edge sends, and that is all
            return await client.run()
        finally:
            await client.close()

    if not config.can_hear:
        renderer.notice("this device has no microphone: it is spoken through")
    try:
        return await stay_connected(
            connect,
            serve,
            renderer,
            backoff=Backoff(first_s=config.reconnect.first_s, max_s=config.reconnect.max_s),
            sleep=sleep,
            chance=chance,
        )
    finally:
        if keeper is not None:
            await keeper.close()
