"""``factotum-device``: the device application.

    factotum-device init                       write a file of settings to start from
    factotum-device doctor                     try the settings, the sound card and the edge, and
                                               say what is wrong
    factotum-device devices                    list the microphones and speakers of this machine
    factotum-device wake-word download [WORD]  fetch the models of a wake word, once
    factotum-device run                        be the device (what ``factotum-device`` alone does)

The settings are in a file (``--config``, ``$FACTOTUM_DEVICE_CONFIG``, else ``%APPDATA%\\factotum\\
device.yaml`` on Windows and ``~/.config/factotum/device.yaml`` elsewhere); the credential of the
device (and the key of Porcupine, when that is what listens for the word) are better in the
environment (``$FACTOTUM_DEVICE_TOKEN``, ``$PICOVOICE_ACCESS_KEY``) than in a file that may be
copied somewhere. With a key, a person presses Enter to speak and is heard until they are quiet (or
press it again); with a wake word, they say it.

The exit code says how it ended: 0 when the person left, 1 for settings or hardware that are wrong,
and, when the edge made clear that it is no use asking again, 3 (it does not accept the device), 4
(another connection of the same device took its place) or 5 (it speaks another version of the
protocol).
"""

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

import httpx

from factotum.client_audio import (
    DEFAULT_OPENWAKEWORD,
    WAKE_WORDS,
    AudioBackend,
    AudioUnavailableError,
    SoundDeviceBackend,
    WakeWordError,
    WakeWordModels,
)
from factotum.client_audio.inputs import Presses
from factotum.client_core import Ending, Outcome, WebSocketConnection
from factotum.device.app import EngineFactory, OpenConnection, build_hardware, engine_of, run_device
from factotum.device.config import (
    CONFIG_VARIABLE,
    Config,
    ConfigError,
    Mode,
    default_path,
    load_config,
)
from factotum.device.doctor import examine, report_of
from factotum.device.render import DeviceRenderer
from factotum.device.wakewords import TIMEOUT, DownloadError, download

EXIT_CODES = {
    Ending.LEFT: 0,
    Ending.REFUSED: 3,
    Ending.REPLACED: 4,
    Ending.UNSUPPORTED: 5,
    Ending.CLOSED: 6,
}

EXAMPLE = """\
# The settings of a Factotum device. Write it once, and check it with ``factotum-device doctor``.
# The credential is better in the environment ($FACTOTUM_DEVICE_TOKEN) than in this file.

edge:
  url: ws://127.0.0.1:8084/v1/connect      # where the edge is (wss:// behind a proxy)

device:
  hardware_id: kitchen-01                  # the label this device was paired under
  # token: ...                             # the credential it was given when it was paired
  capabilities: [audio_in, audio_out]      # what this hardware has: [audio_out] alone is a speaker
                                           # that is spoken through, with no microphone

audio:
  input_device: null                       # a number (``devices`` lists them, and a number is the
  output_device: null                      # surest: the same hardware is listed by each host API)
                                           # or a part of the name; null is the system's default
  volume: 100                              # how loud, in percent (a skill can change it)
  volume_step: 10                          # how far "turn it up" goes, in percent, when nobody
                                           # says by how much: the step of this device

listening:
  mode: push_to_talk                       # Enter begins, being quiet ends it (or Enter again);
                                           # or wake_word, which listens for a word and is the one
                                           # for a service that nobody sits at
  engine: openwakeword                     # what listens for the word: no account and no key, and
                                           # its models are fetched once by ``factotum-device
                                           # wake-word download``; or porcupine, which needs an
                                           # AccessKey that Picovoice gives to companies
  # wake_word: hey_jarvis                  # alexa, hey_jarvis, hey_mycroft or hey_rhasspy (the
                                           # words are English; Porcupine has its own, ``jarvis``)
  threshold: 0.5                           # the score at which the word is taken to have been said:
                                           # lower it if it is not heard (``doctor --word`` says how
                                           # near it came), raise it if a sound is taken for it
  # patience: 1                            # the frames of 80 ms in a row that must be over it
  # models_dir: ...                        # where the models are (~/.cache/factotum/openwakeword)
  # access_key: ...                        # porcupine only: or $PICOVOICE_ACCESS_KEY
  silence_seconds: 1.2                     # how long a person is quiet before it is over
  silence_level: 150                       # the level, of 32768, under which a sound is quiet
  max_seconds: 12                          # the most that one thing said may last
  listen_while_answering: false            # true only with headphones: a speaker hears itself

timekeeping:
  snooze_minutes: 10                       # how long an alarm is put off when nobody says for
                                           # how long: the snooze of this device

reconnect:
  first_s: 1                               # a device that loses the edge asks again after this,
  max_s: 30                                # and after twice as long each time, up to this

log_level: INFO
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factotum-device",
        description="The Factotum device application: a speaker with a microphone.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=f"the file of settings (default: ${CONFIG_VARIABLE}, else the folder of settings of "
        "the person who runs it)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="do not write the words of the answers"
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="be the device (the default)")
    doctor = commands.add_parser(
        "doctor", help="try the settings, the sound card and the edge, and say what is wrong"
    )
    doctor.add_argument(
        "--listen", action="store_true", help="also hear for two seconds and say how loud it was"
    )
    doctor.add_argument(
        "--play", action="store_true", help="also play a short tone, quietly (nobody is warned)"
    )
    doctor.add_argument(
        "--word",
        action="store_true",
        help="also listen for the wake word for five seconds and say how near it came (say it "
        "when asked); this is how to choose listening.threshold",
    )
    doctor.add_argument(
        "--no-edge",
        action="store_true",
        help="do not connect to the edge (which replaces a device of the same hardware id that "
        "is running)",
    )
    commands.add_parser("devices", help="list the microphones and speakers of this machine")
    init = commands.add_parser("init", help="write a file of settings to start from")
    init.add_argument("--force", action="store_true", help="replace the file if there is one")
    wake_word = commands.add_parser(
        "wake-word", help="the models of the words that can be listened for"
    )
    actions = wake_word.add_subparsers(dest="action", required=True)
    fetch = actions.add_parser(
        "download",
        help="fetch the models of a word from where its author published them (once, and checked)",
    )
    fetch.add_argument(
        "word",
        nargs="?",
        default=DEFAULT_OPENWAKEWORD,
        help=f"one of {', '.join(sorted(WAKE_WORDS))} (default: {DEFAULT_OPENWAKEWORD})",
    )
    fetch.add_argument(
        "--dir",
        type=Path,
        help="where to keep them (default: ~/.cache/factotum/openwakeword; the device looks there, "
        "or where listening.models_dir says)",
    )
    return parser


def _say(out: TextIO, line: str) -> None:
    out.write(f"{line}\n")
    out.flush()


def _at_a_keyboard() -> bool:
    """Whether somebody could press Enter: a service, a pipe and a closed input could not."""
    return sys.stdin is not None and sys.stdin.isatty()


def settings(
    arguments: argparse.Namespace, environ: Mapping[str, str], system: str
) -> tuple[Path, Config | None, str | None]:
    """The path of the file, and what it says, or what is wrong with it."""
    path: Path = arguments.config or default_path(environ, system)
    try:
        return path, load_config(path, environ), None
    except ConfigError as error:
        return path, None, str(error)


async def main_async(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    out: TextIO | None = None,
    backend: AudioBackend | None = None,
    presses: Presses | None = None,
    opening: OpenConnection | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    engine: EngineFactory = engine_of,
) -> int:
    """What ``factotum-device`` does, and the exit code that says how it went. Everything that is
    hardware or a network is given, for a test (``engine`` is what listens for the word); nothing
    is given when it is run."""
    arguments = build_parser().parse_args(argv)
    variables = os.environ if environ is None else environ
    platform = sys.platform if system is None else system
    stream = out or sys.stdout
    command = arguments.command or "run"

    if command == "init":
        return _init(arguments, variables, platform, stream)
    if command == "wake-word":
        return await _wake_word(arguments, stream, transport)
    sound = backend or SoundDeviceBackend()
    if command == "devices":
        return await _devices(sound, stream)

    _, config, problem = settings(arguments, variables, platform)
    if config is None:
        _say(stream, f"factotum-device: {problem}")
        return 1
    logging.basicConfig(format="%(asctime)s %(name)s %(message)s")
    logging.getLogger().setLevel(config.log_level)  # basicConfig leaves it when a handler is there
    if command == "doctor":
        return await _doctor(arguments, config, sound, opening, stream, engine)
    return await _run(arguments, config, sound, presses, opening, stream, engine)


def _init(
    arguments: argparse.Namespace, environ: Mapping[str, str], system: str, out: TextIO
) -> int:
    path: Path = arguments.config or default_path(environ, system)
    if path.exists() and not arguments.force:
        _say(out, f"factotum-device: {path} is there already (--force replaces it)")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(EXAMPLE.encode("utf-8"))
    _say(out, f"wrote {path}: put the hardware id and the credential of the device in it")
    return 0


async def _wake_word(
    arguments: argparse.Namespace, out: TextIO, transport: httpx.AsyncBaseTransport | None
) -> int:
    models = WakeWordModels(arguments.dir)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT) as client:
            await download(models, arguments.word, client=client, say=lambda line: _say(out, line))
    except (WakeWordError, DownloadError) as error:
        _say(out, f"factotum-device: {error}")
        return 1
    _say(
        out,
        f"the models of {arguments.word} are in {models.directory}: they are their author's "
        "(CC BY-NC-SA 4.0, for use that is not commercial), https://github.com/dscripka/openWakeWord",
    )
    _say(out, "try them with: factotum-device doctor --word")
    return 0


async def _devices(backend: AudioBackend, out: TextIO) -> int:
    try:
        listed = await asyncio.to_thread(backend.devices)
    except AudioUnavailableError as error:
        _say(out, f"factotum-device: {error}")
        return 1
    default_in, default_out = backend.default_input(), backend.default_output()
    for device in listed:
        marks = []
        if device.index == default_in:
            marks.append("default microphone")
        if device.index == default_out:
            marks.append("default speaker")
        kind = "/".join(
            name for name, count in (("in", device.inputs), ("out", device.outputs)) if count
        )
        suffix = f"  <- {', '.join(marks)}" if marks else ""
        _say(out, f"{device.index:>3}  {device.name}  [{device.api}; {kind or '-'}]{suffix}")
    return 0


async def _doctor(
    arguments: argparse.Namespace,
    config: Config,
    backend: AudioBackend,
    opening: OpenConnection | None,
    out: TextIO,
    engine: EngineFactory,
) -> int:
    checks = await examine(
        config,
        backend,
        listen=arguments.listen,
        play=arguments.play,
        word=arguments.word,
        edge=not arguments.no_edge,
        opening=opening or WebSocketConnection.open,
        engine=engine,
        say=lambda line: _say(out, line),
    )
    _say(out, report_of(checks))
    return 0 if all(check.ok for check in checks) else 1


async def _run(
    arguments: argparse.Namespace,
    config: Config,
    backend: AudioBackend,
    presses: Presses | None,
    opening: OpenConnection | None,
    out: TextIO,
    engine: EngineFactory,
) -> int:
    renderer = DeviceRenderer(out, quiet=arguments.quiet)
    hardware = build_hardware(config, backend, renderer, presses=presses, engine=engine)
    if config.listening.mode is Mode.PUSH_TO_TALK and config.can_hear:
        if presses is None and not _at_a_keyboard():
            renderer.notice(
                "standard input is not a terminal, so there is no key to press: a device that "
                "nobody sits at listens for a wake word (listening.mode)"
            )
        else:
            renderer.notice(
                "press Enter to speak, and Enter again when you are done (q and Enter: quit)"
            )
    try:
        outcome: Outcome = await run_device(
            config, hardware, renderer, opening=opening or WebSocketConnection.open
        )
    except AudioUnavailableError as error:
        _say(out, f"factotum-device: the sound hardware failed: {error} (``doctor`` tries it)")
        return 1
    except WakeWordError as error:
        _say(out, f"factotum-device: {error}")
        return 1
    if not outcome.succeeded:
        _say(out, f"factotum-device: {outcome.ending.value}: {outcome.detail}")
    return EXIT_CODES[outcome.ending]


def main(argv: Sequence[str] | None = None) -> None:
    try:
        code = asyncio.run(main_async(argv))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
