"""``factotum-terminal``: a device in a terminal.

It connects to the edge as the device of a profile (a speaker, a screen, a phone...). What is typed
is what a person said to it; what the edge sends comes out as what that device would do. Typing
while an answer is coming is speaking over it: the answer is interrupted, as it would be.

    factotum-terminal --profile speaker
    factotum-terminal --profile smart-display --url ws://127.0.0.1:8084/v1/connect

Lines that begin with a slash are for the terminal and not for the assistant: ``/help``.

With ``--voice`` (a profile with ``audio_in`` only) a real microphone and speaker replace the
keyboard and the screen: say the wake word, then what you would say, and wait for the answer
before saying it again. What you say is sent to the edge as you say it, a chunk at a time, and the
answer is interrupted by the edge when you begin (ADR 0022). It needs a free Picovoice AccessKey
(``$PICOVOICE_ACCESS_KEY``, from https://console.picovoice.ai/) for the wake-word engine.

    factotum-terminal --profile speaker --voice

With ``--wav-in`` the recordings are in files (16-bit mono WAV) and are said one after the other,
each as if said after the wake word and in the same chunks a microphone would send: the client that
tests the whole stack with no hardware. ``--wav-out`` writes what the assistant says to a WAV file
instead of a speaker, and works with either.

    factotum-terminal --profile speaker --wav-in question.wav --wav-out answer.wav
"""

import argparse
import asyncio
import os
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from factotum.client_audio import (
    ACCESS_KEY_VARIABLE,
    DEFAULT_WAKE_WORD,
    SoundDeviceBackend,
    Speaker,
    WakeWord,
)
from factotum.client_core import (
    CannotConverse,
    Connection,
    ConnectionLost,
    CouldNotConnect,
    DeviceClient,
    Refused,
    VoiceInput,
    VoiceLoop,
    VoiceOutput,
    VoiceSink,
    WebSocketConnection,
)
from factotum.contracts.capabilities import Capability
from factotum.terminal.device import TerminalDevice
from factotum.terminal.profiles import DEFAULT_PROFILE, PROFILES, Profile, get_profile
from factotum.terminal.render import ConsoleRenderer
from factotum.terminal.wavfiles import WavFileInput, WavFileOutput

DEFAULT_URL = "ws://127.0.0.1:8084/v1/connect"
TOKEN_VARIABLE = "FACTOTUM_DEVICE_TOKEN"  # noqa: S105 - the name of a variable, not a secret

HELP = """\
Type what you would say. Typing while an answer is coming interrupts it.
  /cancel   stop the answer that is coming
  /state    what the hardware of this device is doing (volume, light, music, screen)
  /caps     what this device can do
  /help     this
  /quit     go (a line with only ctrl-d or ctrl-z does the same)"""


@dataclass(frozen=True, slots=True)
class Options:
    url: str
    profile: Profile
    hardware_id: str | None
    token: str | None
    voice: bool = False
    wake_word: str = DEFAULT_WAKE_WORD
    access_key: str | None = None
    wav_in: tuple[Path, ...] = ()
    wav_out: Path | None = None
    realtime: bool = False


def parse_args(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> Options:
    variables = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        prog="factotum-terminal",
        description="A Factotum device in a terminal.",
        epilog="profiles:\n"
        + "\n".join(f"  {name:<14}{profile.description}" for name, profile in PROFILES.items()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"the edge (default: {DEFAULT_URL})")
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        choices=sorted(PROFILES),
        help=f"the kind of device to be (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--hardware-id",
        help="the hardware id to present (default: the profile's, which is that of the example "
        "file of devices)",
    )
    parser.add_argument(
        "--token",
        help=f"the credential to present (default: ${TOKEN_VARIABLE}, else the profile's, "
        "a development placeholder)",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="use a real microphone and speaker instead of the keyboard and the screen "
        "(needs the profile's audio_in; downloads a wake-word model on first use)",
    )
    parser.add_argument(
        "--wake-word",
        default=DEFAULT_WAKE_WORD,
        help=f"which of Porcupine's built-in keywords to listen for (default: {DEFAULT_WAKE_WORD})",
    )
    parser.add_argument(
        "--access-key",
        help=f"the Picovoice AccessKey for the wake-word engine (default: ${ACCESS_KEY_VARIABLE}; "
        "get a free one at https://console.picovoice.ai/)",
    )
    parser.add_argument(
        "--wav-in",
        action="append",
        type=Path,
        default=[],
        metavar="FILE",
        help="say what is in this WAV file (16-bit mono) as if said after the wake word, streamed "
        "as a microphone would; give it again for each utterance, said in order. Needs the "
        "profile's audio_in, and replaces the keyboard (no --voice)",
    )
    parser.add_argument(
        "--wav-out",
        type=Path,
        metavar="FILE",
        help="write what the assistant says to this WAV file instead of playing it",
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="with --wav-in, hand over each chunk after the time it takes to say it, as a "
        "microphone would, instead of as fast as the edge takes them",
    )
    args = parser.parse_args(argv)
    if args.voice and args.wav_in:
        parser.error("--voice and --wav-in are two ways of hearing a person: choose one")
    if args.realtime and not args.wav_in:
        parser.error("--realtime paces what --wav-in says, and there is none")
    return Options(
        url=args.url,
        profile=get_profile(args.profile),
        hardware_id=args.hardware_id,
        token=args.token or variables.get(TOKEN_VARIABLE),
        voice=args.voice,
        wake_word=args.wake_word,
        access_key=args.access_key or variables.get(ACCESS_KEY_VARIABLE),
        wav_in=tuple(args.wav_in),
        wav_out=args.wav_out,
        realtime=args.realtime,
    )


async def converse(
    client: DeviceClient,
    device: TerminalDevice,
    profile: Profile,
    lines: AsyncIterator[str | None],
    out: TextIO,
) -> ConnectionLost | None:
    """The terminal's side of it: read what is typed until it ends or the connection does. Returns
    how the connection ended, if it was not the person who ended it."""
    listening = asyncio.create_task(client.run())
    typed = aiter(lines)
    lost = False
    try:
        while True:
            line = await _typed_or_lost(typed, listening)
            if line is None:
                # What is typed is over (a file, a pipe) or the connection is. In the first case the
                # answer to the last of it is still due; in the second nothing is, and it is not
                # waited for.
                if client.active is not None:
                    await client.wait_for_turn(client.active)
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if not await _command(line, client, device, profile, out):
                    break
                continue
            try:
                await client.say(line)
            except CannotConverse as error:
                out.write(f"-- {error}\n")
            except ConnectionLost:
                lost = True
                break
            if client.active is not None:
                await asyncio.sleep(0)  # let what the edge sends first come out before the prompt
    finally:
        lost = lost or listening.done()
        await client.close()
        await asyncio.wait({listening})
    return listening.result() if lost else None


async def _typed_or_lost(
    typed: AsyncIterator[str | None], listening: asyncio.Task[ConnectionLost | None]
) -> str | None:
    """The next line, or None when there will be none: what is typed is over, or the connection is
    (which is not to wait for a person to type in order to be noticed)."""
    reading = asyncio.ensure_future(anext(typed, None))
    await asyncio.wait({reading, listening}, return_when=asyncio.FIRST_COMPLETED)
    if reading.done():
        return reading.result()
    reading.cancel()
    await asyncio.wait({reading})
    return None


async def _command(
    line: str, client: DeviceClient, device: TerminalDevice, profile: Profile, out: TextIO
) -> bool:
    """A line for the terminal. False when it is time to go."""
    word = line.split()[0].lower()
    if word in {"/quit", "/exit", "/q"}:
        return False
    if word == "/help":
        out.write(HELP + "\n")
    elif word == "/caps":
        caps = ", ".join(sorted(c.value for c in profile.capabilities))
        screen = (
            f"; screen {profile.display.width}x{profile.display.height}" if profile.display else ""
        )
        out.write(f"-- {profile.name}: {caps}{screen}\n")
    elif word == "/state":
        out.write(f"-- {device.state.describe()}\n")
    elif word == "/cancel":
        if not await client.cancel():
            out.write("-- nothing is being answered\n")
    else:
        out.write(f"-- {word} is not a command of the terminal (/help)\n")
    out.flush()
    return True


async def stdin_lines(stream: TextIO | None = None) -> AsyncIterator[str | None]:
    """What is typed, a line at a time. It is read by a thread that dies with the process, since a
    read that waits for a person cannot be cancelled."""
    stream = stream or sys.stdin
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def read() -> None:
        while True:
            line = stream.readline()
            loop.call_soon_threadsafe(queue.put_nowait, line if line else None)
            if not line:
                return

    threading.Thread(target=read, name="terminal-input", daemon=True).start()
    while True:
        line = await queue.get()
        yield line
        if line is None:
            return


async def run(
    options: Options,
    *,
    lines: AsyncIterator[str | None] | None = None,
    out: TextIO | None = None,
    open_connection: Callable[[str], Awaitable[Connection]] | None = None,
    voice_input: VoiceInput | None = None,
    voice_output: VoiceOutput | None = None,
    voice_sink: VoiceSink | None = None,
) -> int:
    """Connect as the device of the profile and converse. The exit code says how it went.

    ``voice_input``, ``voice_output`` and ``voice_sink`` stand in for a real microphone and speaker
    in tests (a speaker that takes a track of sound as it arrives asks the edge for its voice as a
    stream, ADR 0023); ``--voice`` without them builds the real ones (``factotum.terminal.audio``),
    and ``--wav-in`` and ``--wav-out`` the ones that are files (``factotum.terminal.wavfiles``).
    """
    out = out or sys.stdout
    renderer = ConsoleRenderer(out)
    by_voice = options.voice or bool(options.wav_in)
    if by_voice and Capability.AUDIO_IN not in options.profile.capabilities:
        flag = "--voice" if options.voice else "--wav-in"
        sys.stderr.write(
            f"factotum-terminal: {flag} needs a microphone, and {options.profile.name!r} has none\n"
        )
        return 1
    if options.voice and voice_input is None and options.access_key is None:
        sys.stderr.write(
            "factotum-terminal: --voice needs a Picovoice AccessKey for the wake-word engine: "
            f"get a free one at https://console.picovoice.ai/ and set ${ACCESS_KEY_VARIABLE}\n"
        )
        return 1
    if options.wav_in and voice_input is None:
        try:  # read now: a file that is not one is known before the edge is reached
            voice_input = WavFileInput(*options.wav_in, realtime=options.realtime)
        except (OSError, ValueError) as error:
            sys.stderr.write(f"factotum-terminal: {error}\n")
            return 1
    opening = open_connection or WebSocketConnection.open
    try:
        connection = await opening(options.url)
    except CouldNotConnect as error:
        sys.stderr.write(f"factotum-terminal: {error}\n")
        return 1
    speaker = voice_output
    sink = voice_sink
    wav_out: WavFileOutput | None = None
    if speaker is None and sink is None:
        if options.wav_out is not None:
            speaker = sink = wav_out = WavFileOutput(options.wav_out)
        elif options.voice:
            speaker = sink = Speaker(SoundDeviceBackend())
    device = TerminalDevice(options.profile.capabilities, renderer)
    client = DeviceClient(
        connection,
        options.profile,
        renderer,
        device,
        hardware_id=options.hardware_id,
        token=options.token,
        voice_output=speaker,
        voice_sink=sink,
    )
    try:
        await client.connect()
    except Refused as refused:
        sys.stderr.write(f"factotum-terminal: the edge refused this device: {refused}\n")
        await connection.close()
        return 1
    if by_voice:
        if options.voice:
            renderer.notice(f"listening for the wake word {options.wake_word!r}")
            if voice_input is None:
                assert options.access_key is not None  # noqa: S101 - checked above
                voice_input = WakeWord(
                    SoundDeviceBackend(),
                    access_key=options.access_key,
                    wake_word=options.wake_word,
                )
        else:
            said = len(options.wav_in)
            renderer.notice(
                f"saying {said} recording{'s' if said != 1 else ''} from files, as if said "
                "after the wake word"
            )
        assert voice_input is not None  # noqa: S101 - made above, or given
        lost = await VoiceLoop(voice_input).serve(client)
    else:
        if Capability.AUDIO_IN in options.profile.capabilities:
            renderer.notice("type what you would say (/help)")
        else:
            renderer.notice(
                "this device cannot start a conversation: it shows what it is sent (/help)"
            )
        typed = lines if lines is not None else stdin_lines()
        lost = await converse(client, device, options.profile, typed, out)
    if wav_out is not None:
        renderer.notice(
            f"wrote {wav_out.seconds:.1f} s of what was said to {wav_out.path}"
            if wav_out.seconds
            else f"nothing was said, so {wav_out.path} was not written"
        )
    if lost is not None:
        why = f": {lost.reason}" if lost.reason else ""
        renderer.notice(f"the connection to the edge ended ({lost.code}{why})")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    options = parse_args(argv)
    try:
        code = asyncio.run(run(options))
    except KeyboardInterrupt:
        code = 0
    raise SystemExit(code)
