"""Timers and alarms, end to end (ADR 0029): somebody tells a device to ring after some minutes or
at an hour, and the device keeps the time on a clock of its own and rings by itself, with a voice
that the platform made when it was set.

The real code decides everything: the actions service reads the words of the person with code (a
model does no sums), the voice of the ring is made by the model gateway and travels with the command
through the edge to the connection of the device, whose keeper holds it, works out which day it is
on its own clock and says it when the time is up. What is made up is the small model that reads the
message, the voice of Piper (a clip that carries its words, so that a test reads what a speaker was
made to say) and the clock of the device, which moves when a test says so.
"""

import asyncio
from datetime import datetime
from typing import Any

from factotum.client_core.timekeeping import RING_EVERY_S
from factotum.contracts.actions import ActCode, ActStatus, AuditList
from factotum.contracts.audit import ActionResult
from factotum.contracts.edge import TurnStatus
from factotum.contracts.timekeeping import TimeState, answer_in
from factotum.e2e.devices import Device, connected, until
from factotum.e2e.house import House, running


def proposal(skill: str, language: str = "en", **parameters: str) -> dict[str, Any]:
    """What a competent planner proposes for a message about a timer or an alarm."""
    return {
        "reason": "it asks for something to be kept",
        "skill": skill,
        "device": None,
        "parameters": parameters,
        "language": language,
        "question": None,
        "ask": None,
    }


def kept(device: Device) -> TimeState:
    """What the device holds: the wall it shows, its timers and its alarms."""
    assert device.timekeeper is not None
    state = answer_in(TimeState, device.timekeeper.state())
    assert state is not None
    return state


async def rung(device: Device, times: int = 1) -> None:
    """Waits until the device has said what it rings with, that many times, and has finished."""
    await until(lambda: len(device.spoken()) >= times)
    await asyncio.wait_for(device.client.wait_until_played(), device.timeout_s)


async def trail(house: House) -> AuditList:
    listed = await house.actions.get("/v1/audit", params={"limit": 50}, headers=house.admin)
    assert listed.status_code == 200, listed.text
    return AuditList.model_validate(listed.json())


class TestATimer:
    async def test_it_is_kept_by_the_device_and_rings_by_itself_when_it_is_up(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("timer", duration="five minutes")

            ended = await speaker.says_aloud("Set a timer for five minutes.")

            assert ended.status is TurnStatus.FINISHED, ended
            assert speaker.spoken() == ["Timer set for 5 minutes."]
            [timer] = kept(speaker).timers
            assert (timer.seconds, timer.left_s, timer.ringing) == (300, 300, False)

            await speaker.clock.advance(299)
            assert speaker.spoken() == ["Timer set for 5 minutes."]  # not yet
            await speaker.clock.advance(1)
            await rung(speaker, 2)

            assert speaker.spoken()[1:] == ["Your timer for 5 minutes is done."]
            assert speaker.alerts() == [False, True]  # the answer is muted by a mute; the ring not
            assert speaker.shown.of("violation") == []

    async def test_it_goes_on_ringing_until_it_is_stopped(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("timer", duration="a minute")
            await speaker.says_aloud("Set a timer for a minute.")
            await speaker.clock.advance(60)
            await rung(speaker, 2)

            await speaker.clock.advance(RING_EVERY_S + 2)
            await rung(speaker, 3)

            assert speaker.spoken()[1:] == ["Your timer for 1 minute is done."] * 2

    async def test_saying_stop_cancels_it_and_it_rings_no_more(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("timer", duration="a minute")
            await speaker.says_aloud("Set a timer for a minute.")
            await speaker.clock.advance(60)
            await rung(speaker, 2)

            house.model.proposal = proposal("timer-cancel")
            await speaker.says_aloud("Stop the timer.")
            await speaker.clock.advance(RING_EVERY_S * 4)
            await asyncio.wait_for(speaker.client.wait_until_played(), speaker.timeout_s)

            assert speaker.spoken()[-1] == "The timer for 1 minute is cancelled."
            assert speaker.spoken().count("Your timer for 1 minute is done.") == 1
            assert kept(speaker).timers == ()

    async def test_the_length_that_a_person_said_in_words_is_the_one_that_is_kept(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("timer", "it", duration="un'ora e mezza")

            await speaker.says_aloud("Metti un timer di un'ora e mezza.")

            assert speaker.spoken() == ["Timer impostato per 1 ora e 30 minuti."]
            [timer] = kept(speaker).timers
            assert timer.seconds == 5400

    async def test_how_much_is_left_is_asked_of_the_device_and_said_from_what_it_answers(
        self,
    ) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("timer", duration="five minutes")
            await speaker.says_aloud("Set a timer for five minutes.")
            await speaker.clock.advance(26)

            house.model.proposal = proposal("timer-status")
            await speaker.says_aloud("How much time is left on the timer?")

            assert speaker.spoken()[-1] == "The timer has 4 minutes and 34 seconds left."

    async def test_a_timer_belongs_to_the_device_that_was_spoken_to(self) -> None:
        async with (
            running(offer_every_skill=True) as house,
            connected(house, "speaker") as speaker,
            connected(house, "smart-display") as display,
        ):
            house.model.proposal = proposal("timer", duration="five minutes")
            await speaker.says_aloud("Set a timer for five minutes.")

            assert len(kept(speaker).timers) == 1
            assert kept(display).timers == ()
            await display.clock.advance(3600)  # the clock of another device is not this one's
            assert display.spoken() == []

    async def test_a_length_that_is_not_understood_is_said_and_nothing_is_set(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("timer", duration="a while")

            await speaker.says_aloud("Set a timer for a while.")

            assert speaker.spoken() == ["I did not understand how long that is."]
            assert kept(speaker).timers == ()
            [record] = (await trail(house)).records
            assert record.result is ActionResult.FAILED

    async def test_the_trail_says_what_was_set(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("timer", duration="five minutes")

            await speaker.says_aloud("Set a timer for five minutes.")

            [record] = (await trail(house)).records
            assert (record.skill_name, record.result, record.detail) == (
                "timer",
                ActionResult.EXECUTED,
                "timer set for 300 s",
            )
            [outcome] = house.acts
            assert (outcome.status, outcome.code) == (ActStatus.DONE, None)

    async def test_a_device_that_plays_no_clips_cannot_ring_and_the_person_is_told(self) -> None:
        async with (
            running(offer_every_skill=True) as house,
            connected(house, "speaker", plays_clips=False) as speaker,
        ):
            house.model.proposal = proposal("timer", duration="five minutes")

            await speaker.says_aloud("Set a timer for five minutes.")

            [outcome] = house.acts
            assert (outcome.status, outcome.code) == (ActStatus.FAILED, ActCode.COMMAND_FAILED)
            assert kept(speaker).timers == ()
            assert speaker.shown.of("violation") == []  # the edge did not send what it cannot do


class TestAnAlarm:
    async def test_it_is_for_the_next_morning_by_the_calendar_of_the_device(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("alarm", time="seven")

            await speaker.says_aloud("Set an alarm for seven.")

            assert speaker.spoken() == ["Alarm set for tomorrow at 7 AM."]
            [alarm] = kept(speaker).alarms
            assert alarm.at == datetime(2026, 10, 14, 7, 0)  # noqa: DTZ001 - civil time

    async def test_it_rings_when_the_wall_of_the_device_says_the_hour(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("alarm", "it", time="sette", day="domani")
            await speaker.says_aloud("Svegliami domani alle sette.")
            assert speaker.spoken() == ["Sveglia impostata per domani alle 7."]

            speaker.clock.set_wall(datetime(2026, 10, 14, 6, 59, 0))  # noqa: DTZ001
            await speaker.clock.advance(30)
            assert speaker.spoken() == ["Sveglia impostata per domani alle 7."]  # not yet
            await speaker.clock.advance(31)
            await rung(speaker, 2)

            assert speaker.spoken()[1:] == ["Sveglia! È ora."]

    async def test_the_day_that_a_person_said_is_the_devices_to_work_out(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("alarm", time="eight", day="monday")

            await speaker.says_aloud("Set an alarm for monday at eight.")

            assert speaker.spoken() == ["Alarm set for Monday at 8 AM."]
            [alarm] = kept(speaker).alarms
            assert alarm.at == datetime(2026, 10, 19, 8, 0)  # noqa: DTZ001

    async def test_a_moment_that_has_gone_by_is_said_and_nothing_is_set(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("alarm", "it", time="7", day="oggi")

            await speaker.says_aloud("Metti la sveglia per oggi alle 7.")

            assert speaker.spoken() == ["Quell'ora è già passata."]
            assert kept(speaker).alarms == ()

    async def test_saying_cancel_stops_it_and_it_is_said(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("alarm", time="seven")
            await speaker.says_aloud("Set an alarm for seven.")

            house.model.proposal = proposal("alarm-cancel")
            await speaker.says_aloud("Cancel the alarm.")

            assert speaker.spoken()[-1] == "The alarm for tomorrow at 7 AM is cancelled."
            assert kept(speaker).alarms == ()

    async def test_what_is_set_is_asked_of_the_device_and_said(self) -> None:
        async with running(offer_every_skill=True) as house, connected(house, "speaker") as speaker:
            house.model.proposal = proposal("alarm", time="seven thirty")
            await speaker.says_aloud("Set an alarm for seven thirty.")

            house.model.proposal = proposal("alarm-status")
            await speaker.says_aloud("What alarms do I have?")

            assert speaker.spoken()[-1] == "Your alarm is set for tomorrow at 7:30 AM."

    async def test_a_shared_display_keeps_its_own_and_nobody_elses(self) -> None:
        async with (
            running(offer_every_skill=True) as house,
            connected(house, "speaker") as speaker,
            connected(house, "smart-display") as display,
        ):
            house.model.proposal = proposal("alarm", time="seven")
            await display.says_aloud("Set an alarm for seven.")

            assert kept(speaker).alarms == ()
            assert len(kept(display).alarms) == 1
