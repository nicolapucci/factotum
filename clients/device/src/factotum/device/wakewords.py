"""Having the models of a wake word, which are what ``factotum-device wake-word download`` is for.

The models of openWakeWord are not in this repository (they are their author's, CC BY-NC-SA 4.0, for
use that is not commercial), and nothing fetches them that is not told to: a person runs the command
once, on each machine that a device runs on, and the files are checked against the size and the
SHA-256 that the library knows them by (``factotum.client_audio.wakeword``) before they are kept. A
file that is there already and is what it should be is left as it is, and one that is not (cut
short, or another file with its name) is fetched again.
"""

import hashlib
from collections.abc import Callable

import httpx

from factotum.client_audio import ModelFile, WakeWordModels

TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class DownloadError(Exception):
    """A file could not be had: it could not be fetched, or what came is not what it should be."""


def size_of(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB" if size >= 100_000 else f"{size / 1_000:.1f} kB"


async def download(
    models: WakeWordModels,
    word: str,
    *,
    client: httpx.AsyncClient,
    say: Callable[[str], None] = lambda line: None,
) -> list[ModelFile]:
    """The files that ``word`` needs and that were not there, now are. Returns the ones that were
    fetched; ``say`` is told what is being done, a line at a time."""
    fetched: list[ModelFile] = []
    for file in models.files_for(word):  # a word with no model is said before anything is fetched
        if models.holds(file):
            say(f"{file.name}: there already")
            continue
        say(f"{file.name} ({size_of(file.size)}) from {file.url}")
        await _fetch(file, models, client)
        fetched.append(file)
    return fetched


async def _fetch(file: ModelFile, models: WakeWordModels, client: httpx.AsyncClient) -> None:
    body = bytearray()
    try:
        async with client.stream("GET", file.url, follow_redirects=True) as response:
            if response.status_code != 200:
                raise DownloadError(f"{file.name}: {file.url} answered {response.status_code}")
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > file.size:  # it is not the file, and it may be a very big one
                    break
    except httpx.HTTPError as error:
        raise DownloadError(
            f"{file.name}: could not be fetched from {file.url} ({type(error).__name__})"
        ) from None
    if len(body) != file.size or hashlib.sha256(body).hexdigest() != file.sha256:
        raise DownloadError(
            f"{file.name}: what came is not the file that was expected (it should be "
            f"{file.size} bytes, with the SHA-256 that the library knows): nothing was kept"
        )
    target = models.path_of(file)
    part = target.with_name(f"{target.name}.part")
    try:
        models.directory.mkdir(parents=True, exist_ok=True)
        try:
            part.write_bytes(bytes(body))
            part.replace(target)  # a file is there whole or it is not there
        finally:
            part.unlink(missing_ok=True)
    except OSError as error:
        raise DownloadError(
            f"{file.name}: could not be kept in {models.directory} "
            f"({error.strerror or type(error).__name__})"
        ) from None
