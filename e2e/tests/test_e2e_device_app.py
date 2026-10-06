"""The device application against the whole assistant: ``factotum-device`` as it will be on a PC
or a Raspberry Pi, with a sound card that a test plays, on a real connection to the edge, and the
services behind it as they are in the other end-to-end tests.

A person presses the key and says something (the blocks that the microphone hands over carry the
words of the stand-in speech), the application streams it as they say it, the answer comes back as
the voice of the assistant and is given to the speaker as it arrives, and a command that a skill
answers with is run by the hands of the device and is what the speaker is told. What is not here
is the hardware: what a sound card does is ``libs/client_audio``'s to test, and what a person hears
is for a person who runs ``factotum-device doctor --play``.
"""

import asyncio
import struct
from typing import Any

import numpy as np
import pytest

from factotum.actions.instructions.testing import plan_dict, step_command
from factotum.client_audio.testing import FakeBackend, KeyPresses
from factotum.client_core import Ending
from factotum.client_core.testing import RecordingRenderer
from factotum.contracts.actions import ActStatus
from factotum.contracts.audio import pcm_of
from factotum.contracts.knowledge import CollectionOwner
from factotum.device.app import build_hardware, run_device
from factotum.device.config import Config
from factotum.device.doctor import examine
from factotum.device.testing import a_config, until
from factotum.e2e import house as the_house
from factotum.e2e.house import House, running
from factotum.e2e.models import ROUTER, SPOKEN_ANSWER
from factotum.e2e.stores import Store
from factotum.e2e.voice import recording_of

BLOCK = 512
PAIRED_AS = {"hardware_id": "kitchen-01", "token": "kitchen-dev-token"}
"""The kitchen speaker of the example home, and the credential that its file of devices gives it."""


@pytest.fixture(
    autouse=True,
    params=[
        pytest.param(Store.MEMORY, id="memory"),
        pytest.param(Store.POSTGRES, id="postgres", marks=pytest.mark.integration),
    ],
)
def _how_the_house_is_run(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Store:
    """What a store does and what a sound card does have nothing to do with each other: the
    carriers of the other tests are not tried here (the application streams, and takes the voice as
    a stream), and the stores are."""
    monkeypatch.setattr(the_house, "DEFAULT_STORE", request.param)
    return request.param  # type: ignore[no-any-return]


def blocks_saying(words: str) -> list[Any]:
    """What a microphone hands over while somebody says ``words``, and then is quiet: the words of
    the stand-in speech, and four blocks of silence that end what is said."""
    pcm, _ = pcm_of(recording_of(words).data)
    samples = np.frombuffer(pcm, dtype="<i2")
    padded = np.concatenate([samples, np.zeros(-len(samples) % BLOCK, dtype="<i2")])
    return [padded[start : start + BLOCK] for start in range(0, len(padded), BLOCK)] + [
        np.zeros(BLOCK, dtype="<i2")
    ] * 4


def configured(house: House, **sections: dict[str, Any]) -> Config:
    """The kitchen speaker, on the edge of the house, hearing a person who is quiet for a tenth of a
    second as done."""
    return a_config(
        device=PAIRED_AS,
        edge={"url": house.edge_url},
        listening={"silence_seconds": 0.1},
        **sections,
    )


ANNOUNCE_DINNER = {
    "reason": "it asks for an announcement",
    "skill": "announce",
    "device": None,
    "parameters": {"message": "dinner is ready"},
    "language": "en",
    "question": None,
    "ask": None,
}
"""What a competent planner proposes for "tell everyone that dinner is ready"."""

SHOUT = """---
name: shout
description: Say a message out loud on the device that is spoken to.
examples:
  - shout that dinner is ready
target: system
parameters:
  message: {type: string}
required: [message]
effect: reversible
---
Give the device a speak command with the message as its text.
"""
"""A skill that a person taught the assistant: it only gives a device words to say (``speak``), and
what makes them a voice is the edge, as for every skill."""


def words_played(track: Any) -> str:
    """The words that a track of the stand-in voice carries: its samples are the bytes of them."""
    count = len(track.sound) // 2
    return bytes(struct.unpack(f"<{count}h", track.sound[: 2 * count])).rstrip(b"\0").decode()


class Speaking:
    """A person at the device: they press the key, say something, and wait for the answer."""

    def __init__(self, house: House, config: Config) -> None:
        self.house = house
        self.config = config
        self.backend = FakeBackend()
        self.presses = KeyPresses()
        self.shown = RecordingRenderer()
        hardware = build_hardware(config, self.backend, self.shown, presses=self.presses)
        self.speaker = hardware.speaker
        self.running = asyncio.create_task(run_device(config, hardware, self.shown))

    async def said(self, words: str) -> None:
        """Says ``words``, and waits until the answer has been said."""
        before = len(self.backend.outputs)
        opened = len(self.backend.inputs)
        self.presses.press()
        await until(
            lambda: len(self.backend.inputs) > opened and not self.backend.inputs[-1].closed
        )
        for block in blocks_saying(words):
            microphone = self.backend.inputs[-1]
            if not microphone.closed:  # what is said after it was heard to be over is not taken
                microphone.hear(block)
        await until(
            lambda: len(self.backend.outputs) > before and "stop" in self.backend.outputs[-1].calls,
            seconds=self.house.timeout_s,
        )

    def stopped(self) -> bool:
        """Whether every sound that the speaker was given has been played to its end."""
        return all("stop" in track.calls for track in self.backend.outputs)

    def played(self) -> list[str]:
        """The words of each sound that the speaker was given, in the order they were opened."""
        return [words_played(track) for track in self.backend.outputs]

    async def leaves(self) -> Any:
        self.presses.leave()
        return await asyncio.wait_for(self.running, self.house.timeout_s)


class TestAPersonAtTheDevice:
    async def test_a_question_is_heard_answered_from_the_documents_and_said_as_it_arrives(
        self,
    ) -> None:
        async with running() as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            person = Speaking(house, configured(house))
            await until(lambda: len(house.sessions) == 1)

            await person.said("How do I reset the router?")
            outcome = await person.leaves()

            assert outcome.ending is Ending.LEFT
            assert person.shown.of("heard") == ["How do I reset the router?"]
            assert person.shown.of("speech") == SPOKEN_ANSWER
            assert person.shown.of("violation") == []
            [track] = person.backend.outputs
            assert track.sample_rate == 22_050
            assert (
                len(track.sound) >= 2 * 2 * 22_050 // 5
            )  # both sentences, of a fifth of a second or more
            assert track.calls[-2:] == ["stop", "close"]  # it was played to its end
            [microphone] = person.backend.inputs
            assert microphone.closed  # open for what was said, and no longer

    async def test_a_command_is_run_by_the_hands_of_the_device_and_the_speaker_is_turned_down(
        self,
    ) -> None:
        async with running() as house:
            person = Speaking(house, configured(house))
            await until(lambda: len(house.sessions) == 1)
            assert person.speaker is not None
            assert person.speaker.volume == 100

            await person.said("Set the volume to 20.")
            await person.leaves()

            assert person.speaker.volume == 20  # the command went through the edge to this device
            assert "volume 20" in person.shown.of("event")
            assert person.shown.of("speech") == ["Volume set to 20."]
            [job] = house.runner.jobs
            assert job.label == "volume"

    async def test_an_announcement_is_clips_that_the_device_plays_besides_what_it_answers(
        self,
    ) -> None:
        async with running(offer_every_skill=True) as house:
            house.model.proposal = ANNOUNCE_DINNER
            person = Speaking(house, configured(house))
            await until(lambda: len(house.sessions) == 1)

            await person.said("Tell everyone that dinner is ready.")
            await until(lambda: len(person.backend.outputs) == 2 and person.stopped())
            await person.leaves()

            [act] = house.acts
            assert (act.status, act.code, act.reply) == (ActStatus.DONE, None, "Done.")
            assert "announcement: dinner is ready" in person.shown.of("event")
            assert person.shown.of("speech") == ["Done."]  # what a person is answered is an answer
            assert sorted(person.played()) == ["Done.", "dinner is ready"]
            assert house.runner.jobs == []  # the platform did it: nothing was run in a sandbox
            assert person.shown.of("violation") == []

    async def test_a_text_that_a_skill_gives_to_say_is_made_a_voice_by_the_edge_and_played(
        self,
    ) -> None:
        async with running(offer_every_skill=True) as house:
            installed = await house.actions.post(
                "/v1/skills", json={"source": SHOUT}, headers=house.admin
            )
            assert installed.status_code == 201, installed.text
            enabled = await house.actions.post(
                f"/v1/skills/{installed.json()['id']}/enable", headers=house.admin
            )
            assert enabled.status_code == 200, enabled.text
            house.model.proposal = {**ANNOUNCE_DINNER, "skill": "shout"}
            house.model.writing = plan_dict(step_command("say", "speak", text="${value.message}"))
            person = Speaking(house, configured(house))
            await until(lambda: len(house.sessions) == 1)

            await person.said("Shout that dinner is ready.")
            await until(lambda: len(person.backend.outputs) == 2 and person.stopped())
            await person.leaves()

            [act] = house.acts
            assert (act.status, act.code, act.skill) == (ActStatus.DONE, None, "shout")
            assert "announcement: dinner is ready" in person.shown.of("event")
            assert sorted(person.played()) == ["Done.", "dinner is ready"]
            assert [job.label for job in house.runner.jobs] == ["shout"]  # a plan, in a sandbox
            assert person.shown.of("violation") == []


class TestADeviceThatTheEdgeDoesNotKnow:
    async def test_a_wrong_credential_is_refused_and_the_device_does_not_ask_again(self) -> None:
        async with running() as house:
            config = a_config(
                device={"hardware_id": "kitchen-01", "token": "not-its-credential"},
                edge={"url": house.edge_url},
            )
            hardware = build_hardware(
                config, FakeBackend(), RecordingRenderer(), presses=KeyPresses()
            )

            outcome = await asyncio.wait_for(
                run_device(config, hardware, RecordingRenderer()), house.timeout_s
            )

            assert outcome.ending is Ending.REFUSED
            assert "auth_failed" in outcome.detail
            assert len(house.sessions) == 0

    async def test_the_doctor_says_which_setting_to_look_at(self) -> None:
        async with running() as house:
            config = a_config(
                device={"hardware_id": "kitchen-01", "token": "not-its-credential"},
                edge={"url": house.edge_url},
            )

            checks = {check.name: check for check in await examine(config, FakeBackend())}

            assert checks["edge"].ok is False
            assert "auth_failed" in checks["edge"].detail
            assert "hardware_id" in (checks["edge"].advice or "")

    async def test_the_doctor_finds_a_device_that_is_all_right_all_right(self) -> None:
        async with running() as house:
            checks = await examine(configured(house), FakeBackend())

            assert [check.ok for check in checks] == [True] * len(checks)
            assert "voice_stream" in {c.name: c for c in checks}["edge"].detail
