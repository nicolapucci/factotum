"""Saying something on a device, end to end (ADR 0026): somebody tells the assistant to announce
something, and the platform's own skill makes the voice of the message with the model gateway and
has the device that was named play it, while the person who asked is answered on the device they
spoke to.

The real code decides everything: the actions service chooses ``announce`` among the skills that
the platform gives every tenant, authorizes it with the policy of the device that was spoken to,
runs it in its own process (no sandbox: it is the platform's code), asks the gateway for the voice
of each part of the message, and has the edge send each clip to the connection of the device, which
plays it when it is quiet. What is made up is the small model that reads the message and the voice
of Piper (a clip that carries the words it was asked to say, so that a test reads what a speaker was
made to say).
"""

import asyncio
from typing import Any

from factotum.actions.plan_cases import DISPLAY, KITCHEN
from factotum.contracts.actions import ActCode, ActStatus, AuditList
from factotum.contracts.audit import ActionResult
from factotum.contracts.authorization import DecisionOutcome
from factotum.contracts.edge import TurnStatus
from factotum.devices.home import device_id_for
from factotum.e2e.devices import Device, connected
from factotum.e2e.house import House, running
from factotum.e2e.models import VOICE
from factotum.e2e.wiring import TENANT

DINNER = "dinner is ready"
LONG = " ".join(f"Sentence number {n} says that dinner is ready." for n in range(9))
"""423 characters: more than a clip is made of, and less than a person says in one message."""


def proposal(message: str, device: str | None = None, language: str = "en") -> dict[str, Any]:
    """What a competent planner proposes for a message that asks to announce ``message``."""
    return {
        "reason": "it asks for an announcement",
        "skill": "announce",
        "device": device,
        "parameters": {"message": message},
        "language": language,
        "question": None,
        "ask": None,
    }


async def trail(house: House) -> AuditList:
    listed = await house.actions.get("/v1/audit", params={"limit": 50}, headers=house.admin)
    assert listed.status_code == 200, listed.text
    return AuditList.model_validate(listed.json())


async def played(*devices: Device) -> None:
    """Waits until what each device was given to say has been said, as the person who asked does:
    the device that was named is not the one that the answer is given on."""
    for device in devices:
        await asyncio.wait_for(device.client.wait_until_played(), device.timeout_s)


class TestAnAnnouncement:
    async def test_a_message_is_said_on_the_device_that_was_named_and_the_answer_where_it_was_asked(
        self,
    ) -> None:
        async with (
            running(offer_every_skill=True) as house,
            connected(house, "speaker") as speaker,
            connected(house, "smart-display") as display,
        ):
            house.model.proposal = proposal(DINNER, DISPLAY)

            ended = await speaker.says_aloud(
                "Announce on the living room display that dinner is ready."
            )
            await played(display)

            assert ended.status is TurnStatus.FINISHED, ended
            assert display.spoken() == [DINNER]
            assert speaker.spoken() == ["Done."]  # the answer is not the announcement
            assert display.shown.of("violation") == []
            assert speaker.shown.of("violation") == []

    async def test_it_is_said_on_the_device_that_was_spoken_to_when_none_was_named(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal(DINNER)

            await speaker.says_aloud("Tell everyone that dinner is ready.")

            assert sorted(speaker.spoken()) == sorted(["Done.", DINNER])

    async def test_nothing_ran_in_a_sandbox_and_nothing_was_reached_on_the_network(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal(DINNER)

            await speaker.says_aloud("Tell everyone that dinner is ready.")

            assert house.runner.jobs == []
            assert house.home_assistant.seen == []
            assert (
                len(house.model.writings) == 0
            )  # no model wrote a plan for what the platform does

    async def test_the_voice_that_was_asked_for_is_the_one_that_was_bound_and_for_the_message(
        self,
    ) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal(DINNER)

            await speaker.says_aloud("Tell everyone that dinner is ready.")

            assert house.piper.spoken.count(DINNER) == 1  # made once, for the one device
            assert set(house.piper.voices) == {VOICE}  # in the voice that the tenant bound

    async def test_a_long_message_is_said_in_clips_that_say_all_of_it_in_order(self) -> None:
        async with (
            running(offer_every_skill=True) as house,
            connected(house, "speaker") as speaker,
            connected(house, "smart-display") as display,
        ):
            house.model.proposal = proposal(LONG, DISPLAY)

            await speaker.says_aloud("Announce on the living room display what I am about to say.")
            await played(display)

            assert len(display.spoken()) == 2
            assert " ".join(display.spoken()) == LONG
            assert speaker.spoken() == ["Done."]

    async def test_it_is_said_in_italian_when_the_person_spoke_it(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("la cena è pronta", language="it")

            await speaker.says_aloud("Annuncia che la cena è pronta.")

            assert sorted(speaker.spoken()) == sorted(["Fatto.", "la cena è pronta"])

    async def test_the_trail_says_what_was_said_where_and_that_it_was_done(self) -> None:
        async with (
            running(offer_every_skill=True) as house,
            connected(house, "speaker") as speaker,
            connected(house, "smart-display") as display,
        ):
            house.model.proposal = proposal(DINNER, DISPLAY)

            await speaker.says_aloud("Announce on the living room display that dinner is ready.")
            await played(display)

            [record] = (await trail(house)).records
            assert record.skill_name == "announce"
            assert record.params == {"message": DINNER}
            assert record.targets == (device_id_for(TENANT, "living-room-01"),)
            assert (record.decision, record.result) == (
                DecisionOutcome.ALLOW,
                ActionResult.EXECUTED,
            )
            assert record.detail == f"said '{DINNER}' in 1 clip"
            assert "audio" not in record.model_dump_json()  # the words, and never the sound

    async def test_the_platforms_skill_is_there_for_the_tenant_before_a_word_was_said(self) -> None:
        async with running() as house:
            listed = await house.actions.get("/v1/skills", headers=house.admin)

            names = {skill["name"]: skill for skill in listed.json()["skills"]}
            assert names["announce"]["enabled_version"] == 1
            assert names["announce"]["mode"] == "instructions"
            assert names["announce"]["effect"] == "reversible"


class TestWhenItCannotBeSaid:
    async def test_with_no_voice_bound_it_is_a_skill_that_failed_and_nothing_is_said_anywhere(
        self,
    ) -> None:
        async with (
            running(offer_every_skill=True, text_to_speech=False) as house,
            connected(house, "smart-display") as display,
        ):
            house.model.proposal = proposal(DINNER, KITCHEN)

            ended = await display.types("Announce in the kitchen that dinner is ready.")

            assert ended.status is TurnStatus.FINISHED, ended
            [act] = house.acts
            assert (act.status, act.code) == (ActStatus.FAILED, ActCode.SKILL_FAILED)
            [record] = (await trail(house)).records
            assert record.result is ActionResult.FAILED
            assert record.detail is not None
            assert record.detail.startswith("the skill failed: the voice could not be made (")
            assert display.spoken() == []
            assert display.shown.of("text") == ["The announce skill failed."]

    async def test_a_device_that_cannot_play_a_clip_is_given_the_words_to_say(self) -> None:
        async with (
            running(offer_every_skill=True) as house,
            connected(house, "speaker", plays_clips=False) as speaker,
        ):
            house.model.proposal = proposal(DINNER)

            ended = await speaker.types("Tell everyone that dinner is ready.")

            assert ended.status is TurnStatus.FINISHED, ended
            assert speaker.client.features == frozenset()
            assert speaker.hardware.state.spoken == [DINNER]  # as words, which it may say or show
            assert speaker.sentences() == []
            [act] = house.acts
            assert (act.status, act.code) == (ActStatus.DONE, None)

    async def test_a_device_that_is_not_connected_is_a_command_that_was_not_done(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal(DINNER, DISPLAY)  # the display is not connected

            ended = await speaker.says_aloud(
                "Announce on the living room display that dinner is ready."
            )

            assert ended.status is TurnStatus.FINISHED, ended
            [act] = house.acts
            assert (act.status, act.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)
            assert speaker.spoken() == [
                "I asked the device to run the announce skill, and it did not do it."
            ]
