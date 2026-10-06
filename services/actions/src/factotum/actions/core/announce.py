"""``announce``: say something out loud on a device, such as that dinner is ready.

The first skill of the platform (ADR 0026). What is said is not the feedback to the person who asked
(that is "Done.", and goes the way every answer goes): it is content for a device, often another one
than the one they spoke to. So the message is made into sound once, here, by the model gateway, and
the device is given clips to say, in order, which the platform's command ``play_audio`` carries. A
skill of a person cannot give a clip: what it can give is a ``speak``, which the edge makes the
voice of in the same way, for one device.

The message is cut into sentences as an answer is (``factotum.common.sentences``) and the sentences
are put together in blocks of a clip's size, each of which is asked for at the same time as the
others: the device says the clips one after the other as it takes them. What a person says in a
message is a word or a phrase (``MAX_TEXT_CHARS`` of the values of a skill), so it is one clip or
two; what other skills may ask for later can be more, up to ``MAX_SPOKEN_CHARS``.
"""

import asyncio
from typing import ClassVar

from factotum.actions.core.skills import CoreResult, CoreSkill, SpeechMaker
from factotum.common.sentences import sentences_of
from factotum.contracts.commands import MAX_SPOKEN_CHARS, DeviceCommand, PlayAudio
from factotum.contracts.errors import FactotumError, SkillRunError
from factotum.contracts.identity import Owner, Principal
from factotum.contracts.runner import RunRequest

MAX_BLOCK_CHARS = 400
"""How much of a message is one clip: about half a minute of speech, which is far from the 2 MiB
that a clip may be, so that a long message is a few clips and each is made in a few seconds."""

SOURCE = """\
---
name: announce
description: Say something out loud on a device, such as that dinner is ready.
examples:
  - tell the kitchen that dinner is ready
  - announce on the living room display that the guests have arrived
  - say that it is time to go
  - annuncia in cucina che la cena è pronta
  - dì in salotto che è ora di andare
target: system
parameters:
  message: {type: string, description: "what to say, as the person said it"}
required: [message]
effect: reversible
timeout_s: 60
---
A skill of the platform: the message is made into sound once, by the model gateway, and the device
that was named (or the one that was spoken to, when none was) is given the clips to say, one after
the other. Nothing is reached on the network, so nothing is granted.
"""


def blocks_of(message: str) -> list[str]:
    """What a message is made into clips as: its sentences, put together while they fit in a block
    (a sentence is never cut, and is at most ``MAX_CHUNK_CHARS`` long, which is less than a
    block)."""
    blocks: list[str] = []
    for sentence in sentences_of(message):
        if blocks and len(blocks[-1]) + 1 + len(sentence) <= MAX_BLOCK_CHARS:
            blocks[-1] = f"{blocks[-1]} {sentence}"
        else:
            blocks.append(sentence)
    return blocks


class Announce(CoreSkill):
    source: ClassVar[str] = SOURCE

    def __init__(self, speech: SpeechMaker) -> None:
        super().__init__()
        self._speech = speech

    async def run(self, request: RunRequest, principal: Principal) -> CoreResult:
        message = request.params.get("message")
        if not isinstance(message, str) or not message.strip():
            raise SkillRunError("there is nothing to announce")
        if len(message) > MAX_SPOKEN_CHARS:
            raise SkillRunError(f"an announcement is at most {MAX_SPOKEN_CHARS} characters")
        blocks = blocks_of(message)
        if not blocks:
            raise SkillRunError("there is nothing to say in what was asked to be announced")

        owner = Owner.of_subject(principal.acts_as)
        try:
            async with asyncio.TaskGroup() as group:
                made = [
                    group.create_task(self._speech.synthesize(owner, block)) for block in blocks
                ]
        except ExceptionGroup as failures:
            # A voice that could not be made is said once, and not as a group of them: nothing is
            # given to a device when any part of the message has no voice. What the trail says of it
            # is what kind of failure it was (the code of the error), never what the service said.
            failure = failures.exceptions[0]
            if isinstance(failure, FactotumError):
                raise SkillRunError(f"the voice could not be made ({failure.code})") from failure
            raise failure from failures
        commands: tuple[DeviceCommand, ...] = tuple(
            PlayAudio(text=block, audio=clip.result())
            for block, clip in zip(blocks, made, strict=True)
        )
        clips = f"{len(commands)} clip{'s' if len(commands) != 1 else ''}"
        return CoreResult(commands=commands, audit=f"said {' '.join(blocks)!r} in {clips}")
