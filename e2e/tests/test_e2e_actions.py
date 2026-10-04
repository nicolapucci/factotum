"""What is asked to be done, end to end: somebody tells a device to do something, and it is done, or
refused, or asked about, in words that come out of its speaker.

The home is the example one (``config/devices.example.yaml``) with the skills of
``skills/examples``: a kitchen speaker and a living room display that anybody in the home may speak
to (the display may start what cannot be undone, the speaker may not), a phone that is one person's
own, a living room lamp and a garage door that belong to everybody and a bedroom lamp that is that
person's alone.

The real code decides everything: the actions service chooses the skill among those near the
message, checks what the model proposed, authorizes it with the policy of the device that was spoken
to, asks for a yes where it must, runs the script of the skill (a process of its own, with the token
the home keeps) and has the device that was named do what the script answers with, through the edge
and over the connection the device holds. What is made up is the small model that reads the message
(a proposal that is checked), and Home Assistant, which is what the scripts reach: it keeps what it
was asked.
"""

from factotum.contracts.actions import AuditList, SkillList
from factotum.contracts.audit import ActionResult
from factotum.contracts.authorization import DecisionOutcome, Reason
from factotum.contracts.edge import TurnStatus
from factotum.contracts.identity import Subject
from factotum.contracts.knowledge import CollectionOwner
from factotum.devices.home import device_id_for
from factotum.e2e.devices import connected, remembered
from factotum.e2e.house import HA_TOKEN, TENANT, House, running
from factotum.e2e.models import ROUTER, SPOKEN_ANSWER

HOME_ASSISTANT = "http://homeassistant.local:8123"
GARAGE = "Open the garage door."
LAMP = "Turn on the lamp."


def reached(house: House) -> list[str]:
    """What Home Assistant was asked, as the call and the entity, in order."""
    return [
        f"{seen.target.removeprefix(HOME_ASSISTANT)} {seen.json()['entity_id']}"
        for seen in house.home_assistant.seen
    ]


class TestDoingWhatIsAsked:
    async def test_a_lamp_is_switched_on_through_home_assistant_with_the_token_of_the_home(
        self,
    ) -> None:
        async with running() as house, connected(house, "speaker") as speaker:
            ended = await speaker.says_aloud(LAMP)

            assert ended.status is TurnStatus.FINISHED, ended
            [call] = house.home_assistant.seen
            assert (call.method, call.target) == (
                "POST",
                f"{HOME_ASSISTANT}/api/services/light/turn_on",
            )
            assert call.headers["authorization"] == f"Bearer {HA_TOKEN}"
            assert call.json() == {"entity_id": "light.living_room"}
            # The script was given what the skill declared and nothing else: the one secret, and
            # the one host it may reach.
            [job] = house.runner.jobs
            assert (job.label, job.hosts, job.env) == (
                "lamp",
                ("homeassistant.local",),
                {"HA_TOKEN": HA_TOKEN},
            )
            assert speaker.spoken() == ["The lamp is on."]
            assert speaker.shown.of("violation") == []

    async def test_what_was_done_is_remembered_like_any_other_answer(self) -> None:
        async with running() as house, connected(house, "phone") as phone:
            await phone.says_aloud(LAMP)

            [exchange] = await remembered(house)
            assert (exchange.question, exchange.answer) == (LAMP, "The lamp is on.")

    async def test_the_volume_of_the_device_that_was_spoken_to_is_the_one_that_changes(
        self,
    ) -> None:
        async with (
            running() as house,
            connected(house, "speaker") as speaker,
            connected(house, "smart-display") as display,
        ):
            ended = await speaker.says_aloud("Set the volume to 20.")

            assert ended.status is TurnStatus.FINISHED, ended
            # The command went through the edge to the connection of the speaker, which ran it and
            # said so; the other device, which was not spoken to, was left as it was.
            assert speaker.hardware.state.volume == 20
            assert display.hardware.state.volume == 50
            assert speaker.spoken() == ["Volume set to 20."]
            [job] = house.runner.jobs
            assert (job.label, job.hosts, job.env) == ("volume", (), {})  # nothing to reach
            assert house.home_assistant.seen == []

    async def test_a_home_assistant_that_fails_is_said_and_the_next_message_is_not_affected(
        self,
    ) -> None:
        async with running() as house, connected(house, "speaker") as speaker:
            house.home_assistant.answer("POST", "/api/services/light/turn_on", status=500)
            failed = await speaker.says_aloud(LAMP)
            house.home_assistant.answer("POST", "/api/services/light/turn_on", json_body=[])
            done = await speaker.says_aloud(LAMP)

            assert (failed.status, done.status) == (TurnStatus.FINISHED, TurnStatus.FINISHED)
            assert speaker.spoken() == ["It did not work.", "The lamp is on."]


class TestWhatADeviceMayStart:
    async def test_the_garage_door_is_refused_to_the_kitchen_speaker(self) -> None:
        async with running() as house, connected(house, "speaker") as speaker:
            ended = await speaker.says_aloud(GARAGE)

            assert ended.status is TurnStatus.FINISHED, ended
            assert speaker.spoken() == ["This device is not allowed to do that."]
            assert house.home_assistant.seen == []

    async def test_it_is_refused_in_the_language_it_was_asked_in(self) -> None:
        async with running() as house, connected(house, "speaker") as speaker:
            await speaker.says_aloud("Apri il garage.")

            assert speaker.spoken() == ["Questo dispositivo non può farlo."]
            assert house.home_assistant.seen == []

    async def test_the_living_room_display_asks_and_only_a_yes_opens_the_door(self) -> None:
        async with running() as house, connected(house, "smart-display") as display:
            await display.says_aloud(GARAGE)
            asked = list(house.home_assistant.seen)
            await display.says_aloud("Yes.")

            assert asked == []  # nothing moved while it was only a question
            assert display.spoken() == [
                "Do you really want me to open the garage door?",
                "The garage door is opening.",
            ]
            assert reached(house) == ["/api/services/cover/open_cover cover.garage"]

    async def test_a_no_leaves_the_door_as_it_is(self) -> None:
        async with running() as house, connected(house, "smart-display") as display:
            await display.says_aloud(GARAGE)
            await display.says_aloud("No.")

            assert display.spoken()[-1] == "Okay, I will not do it."
            assert house.home_assistant.seen == []

    async def test_a_yes_that_adds_something_is_not_a_yes(self) -> None:
        async with running() as house, connected(house, "smart-display") as display:
            await display.says_aloud(GARAGE)
            await display.says_aloud("Yes, and also turn off the lamp.")

            # The words left a doubt, so the model was asked, and it said it was something else:
            # the question was dropped and the message was a new one, which switched off the lamp.
            assert len(house.model.confirmations) == 1
            assert reached(house) == ["/api/services/light/turn_off light.living_room"]

    async def test_a_yes_is_taken_once(self) -> None:
        async with running() as house, connected(house, "smart-display") as display:
            await display.says_aloud(GARAGE)
            await display.says_aloud("Yes.")
            await display.says_aloud("Yes.")

            assert reached(house) == ["/api/services/cover/open_cover cover.garage"]

    async def test_a_yes_said_to_another_device_confirms_nothing(self) -> None:
        async with (
            running() as house,
            connected(house, "smart-display") as display,
            connected(house, "speaker") as speaker,
        ):
            # Both are the home's, they act for the same, and only one was asked.
            await display.says_aloud(GARAGE)
            await speaker.says_aloud("Yes.")
            moved_by_the_speaker = list(house.home_assistant.seen)
            await display.says_aloud("Yes.")

            assert moved_by_the_speaker == []  # what is asked on a device is waited for on it
            assert reached(house) == ["/api/services/cover/open_cover cover.garage"]

    async def test_a_skill_that_is_disabled_meanwhile_is_not_run_by_a_late_yes(self) -> None:
        async with running() as house, connected(house, "smart-display") as display:
            await display.says_aloud(GARAGE)
            listed = SkillList.model_validate((await house.actions.get("/v1/skills")).json())
            garage_door = next(skill for skill in listed.skills if skill.name == "garage-door")
            disabled = await house.actions.post(f"/v1/skills/{garage_door.id}/disable")
            await display.says_aloud("Yes.")

            # When a yes is taken nothing is assumed: the action is decided again, and a skill that
            # nobody can run any more is not run, whatever was asked a moment ago.
            assert disabled.status_code == 200, disabled.text
            assert display.spoken()[-1] == "That is not available any more."
            assert house.home_assistant.seen == []


class TestWhatAPersonMaySee:
    async def test_a_lamp_that_is_a_persons_own_is_theirs_and_nobody_elses(self) -> None:
        async with (
            running() as house,
            connected(house, "phone") as phone,
            connected(house, "speaker") as speaker,
        ):
            await phone.says_aloud("Turn on my lamp.")
            await speaker.says_aloud("Turn on my lamp.")

            # The model named the bedroom lamp to both. The phone may use it; for the shared speaker
            # it does not exist, and what the speaker is asked is which of its own it means.
            assert reached(house) == ["/api/services/light/turn_on light.bedroom"]
            assert speaker.spoken() == ["Which one do you mean: Living room lamp?"]
            assert "Bedroom" not in str(speaker.shown.shown)


class TestAQuestionIsNotAnAction:
    async def test_a_question_that_names_something_to_do_is_answered_and_nothing_is_run(
        self,
    ) -> None:
        async with running() as house, connected(house, "speaker") as speaker:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            await speaker.says_aloud("How do I reset the router?")

            assert speaker.spoken() == SPOKEN_ANSWER
            assert house.runner.jobs == []
            assert house.home_assistant.seen == []
            # What reads a message to plan an action reads the message alone: the model was asked
            # about it (the router is a skill's business too) and was never shown the manual.
            assert house.model.plans != []
            assert all(ROUTER not in shown for shown in house.model.plans)


class TestTheTrail:
    async def test_every_step_of_an_action_is_kept_with_the_device_it_came_from(self) -> None:
        kitchen = device_id_for(TENANT, "kitchen-01")
        living_room = device_id_for(TENANT, "living-room-01")
        async with (
            running() as house,
            connected(house, "speaker") as speaker,
            connected(house, "smart-display") as display,
        ):
            await speaker.says_aloud(LAMP)
            await speaker.says_aloud(GARAGE)
            await display.says_aloud(GARAGE)
            await display.says_aloud("Yes.")

            trail = AuditList.model_validate((await house.actions.get("/v1/audit")).json())
            steps = [
                (r.skill_name, r.device_id, r.decision, r.result, r.confirmed)
                for r in reversed(trail.records)  # the trail lists the latest first
            ]
            assert steps == [
                ("lamp", kitchen, DecisionOutcome.ALLOW, ActionResult.EXECUTED, None),
                ("garage-door", kitchen, DecisionOutcome.DENY, ActionResult.NOT_EXECUTED, None),
                (
                    "garage-door",
                    living_room,
                    DecisionOutcome.REQUIRE_CONFIRMATION,
                    ActionResult.NOT_EXECUTED,
                    None,
                ),
                (
                    "garage-door",
                    living_room,
                    DecisionOutcome.REQUIRE_CONFIRMATION,
                    ActionResult.EXECUTED,
                    True,  # it ran because it was confirmed, and the trail says so
                ),
            ]
            # Whom a shared device acted for is the tenant, and why it was refused is kept.
            assert {record.acts_as for record in trail.records} == {Subject.tenant(TENANT)}
            assert trail.records[2].reasons == (Reason.EXCEEDS_DEVICE_MAX_EFFECT,)
