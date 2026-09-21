"""``factotum-terminal``: a device in a terminal.

It connects to the edge as the device of a profile (a speaker, a screen, a phone...). What is typed
is what a person said to it; what the edge sends comes out as what that device would do. Typing
while an answer is coming is speaking over it: the answer is interrupted, as it would be.

    factotum-terminal --profile speaker
    factotum-terminal --profile smart-display --url ws://127.0.0.1:8084/v1/connect

Lines that begin with a slash are for the terminal and not for the assistant: ``/help``.
"""

import argparse
import asyncio
import os
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TextIO

from factotum.contracts.capabilities import Capability
from factotum.terminal.client import CannotConverse, DeviceClient, Refused
from factotum.terminal.connection import (
    Connection,
    ConnectionLost,
    CouldNotConnect,
    WebSocketConnection,
)
from factotum.terminal.device import TerminalDevice
from factotum.terminal.profiles import DEFAULT_PROFILE, PROFILES, Profile, get_profile
from factotum.terminal.render import ConsoleRenderer

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
    args = parser.parse_args(argv)
    return Options(
        url=args.url,
        profile=get_profile(args.profile),
        hardware_id=args.hardware_id,
        token=args.token or variables.get(TOKEN_VARIABLE),
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
) -> int:
    """Connect as the device of the profile and converse. The exit code says how it went."""
    out = out or sys.stdout
    renderer = ConsoleRenderer(out)
    opening = open_connection or WebSocketConnection.open
    try:
        connection = await opening(options.url)
    except CouldNotConnect as error:
        sys.stderr.write(f"factotum-terminal: {error}\n")
        return 1
    device = TerminalDevice(options.profile.capabilities, renderer)
    client = DeviceClient(
        connection,
        options.profile,
        renderer,
        device,
        hardware_id=options.hardware_id,
        token=options.token,
    )
    try:
        await client.connect()
    except Refused as refused:
        sys.stderr.write(f"factotum-terminal: the edge refused this device: {refused}\n")
        await connection.close()
        return 1
    if Capability.AUDIO_IN in options.profile.capabilities:
        renderer.notice("type what you would say (/help)")
    else:
        renderer.notice("this device cannot start a conversation: it shows what it is sent (/help)")
    typed = lines if lines is not None else stdin_lines()
    lost = await converse(client, device, options.profile, typed, out)
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
