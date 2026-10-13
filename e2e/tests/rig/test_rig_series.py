"""The rig of a series of commands: what it makes the planner propose, what it makes a model write
for a skill that is only instructions, how it judges what came of a message, what it says the audit
trail must hold, how it tells what was heard from what was said, and what it does around a case.
Nothing here is a service or a store: that a whole series comes to what it is meant to is
``test_e2e_series.py``."""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import replace as changed
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator

from factotum.actions.instructions.checks import check_plan
from factotum.actions.instructions.testing import Seen
from factotum.actions.instructions.writer import RESPONSE_SCHEMA
from factotum.actions.plan_cases import DISPLAY, KITCHEN, PHONE, PLAN_CASES, PlanCase
from factotum.actions.planner import Plan
from factotum.actions.runner_client import hosts_of
from factotum.actions.skills import load_folder
from factotum.client_core import Timekeeper
from factotum.client_core.testing import FakeClock, RecordingRenderer
from factotum.contracts.actions import ActCode, ActOutcome, ActStatus
from factotum.contracts.audit import ActionResult, AuditRecord
from factotum.contracts.authorization import DecisionOutcome
from factotum.contracts.chat import ChatMessage, ChatRequest, MessageRole
from factotum.contracts.edge import TurnEnded, TurnStatus
from factotum.contracts.identity import Subject
from factotum.contracts.ids import AuditId, SkillId
from factotum.contracts.instructions import InstructionPlan
from factotum.contracts.models import ModelRole
from factotum.contracts.runner import Job, JobFile, JobResult
from factotum.contracts.testing import make_tenant_id
from factotum.contracts.timekeeping import TimeState
from factotum.e2e.house import SKILLS_FOLDER, House
from factotum.e2e.models import HouseholdModel, plan_for
from factotum.e2e.runner import HouseRunner
from factotum.e2e.series import (
    ENTITIES,
    NO,
    PROFILES,
    YES,
    Played,
    Series,
    Window,
    as_steps,
    follows_with_a_yes,
    judge,
    likeness,
    nearest,
    plain,
    problems_of,
    proposal_for,
    question_for,
    table_of,
    trail_of,
    weather_of,
    writing_for,
)
from factotum.e2e.wiring import HA_TOKEN

LAMP_ON = PlanCase(
    "turn on the living room lamp",
    KITCHEN,
    "lamp",
    ActStatus.DONE,
    "Living room lamp",
    {"state": "on"},
)
VOLUME = PlanCase("set the volume to 30", KITCHEN, "volume", ActStatus.DONE, None, {"level": 30})
GARAGE_AT_THE_DISPLAY = PlanCase(
    "apri il garage",
    DISPLAY,
    "garage-door",
    ActStatus.NEEDS_CONFIRMATION,
    "Garage door",
    {"action": "open"},
)
GARAGE_AT_THE_PHONE = PlanCase(
    "close the garage",
    PHONE,
    "garage-door",
    ActStatus.NEEDS_CONFIRMATION,
    "Garage door",
    {"action": "close"},
)
GARAGE_AT_THE_KITCHEN = PlanCase(
    "open the garage door",
    KITCHEN,
    "garage-door",
    ActStatus.REFUSED,
    "Garage door",
    {"action": "open"},
)
A_QUESTION = PlanCase("tell me a joke", KITCHEN, None, ActStatus.NO_SKILL)
TOO_LITTLE = PlanCase("turn it up", KITCHEN, "volume", ActStatus.NEEDS_MORE)
MUTED = PlanCase("mute the sound", KITCHEN, "mute", ActStatus.DONE, None, {"muted": True})
UNMUTED = PlanCase("unmute", KITCHEN, "mute", ActStatus.DONE, None, {"muted": False})
HOW_LOUD = PlanCase("what is the volume", KITCHEN, "volume-status", ActStatus.DONE)
TURNED_UP = PlanCase(
    "turn the volume up", KITCHEN, "volume-change", ActStatus.DONE, None, {"direction": "up"}
)
TURNED_DOWN_BY_TWENTY = PlanCase(
    "turn the volume down by 20",
    KITCHEN,
    "volume-change",
    ActStatus.DONE,
    None,
    {"direction": "down", "amount": 20},
)

NAMES = tuple(PROFILES)
NOTHING_KEPT = TimeState(now=datetime(2026, 10, 13, 22, 0))  # noqa: DTZ001 - civil time
LAMP_CALL = "http://homeassistant.local:8123/api/services/light/turn_on"
ENDED = TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED)


def job(label: str, hosts: tuple[str, ...] = (), env: dict[str, str] | None = None) -> Job:
    return Job(
        label=label,
        files=(JobFile.of("run.py", b"print(1)"),),
        entrypoint="run.py",
        hosts=hosts,
        env=env or {},
        timeout_s=10,
    )


def call(target: str, body: dict[str, Any], *, token: str = HA_TOKEN) -> Seen:
    return Seen("POST", target, {"authorization": f"Bearer {token}"}, json.dumps(body).encode())


def handled(runner: HouseRunner, asked: Job) -> JobResult:
    """A job as the runner is asked for it: over the interface of the runner service."""
    request = httpx.Request("POST", "http://runner.test/run", content=asked.model_dump_json())
    return JobResult.model_validate_json(runner.handle(request).text)


def window(**changes: Any) -> Window:
    """A window in which nothing happened, as a message that no skill fits leaves one."""
    fields: dict[str, Any] = {
        "said": "words",
        "heard": "words",
        "ended": ENDED,
        "asked": 1,
        "outcome": ActOutcome.no_skill(),
        "jobs": (),
        "web": (),
        "events": dict.fromkeys(NAMES, ()),
        "announced": dict.fromkeys(NAMES, ()),
        "sentences": dict.fromkeys(NAMES, 0),
        "volumes": dict.fromkeys(NAMES, 50),
        "volumes_before": dict.fromkeys(NAMES, 50),
        "muted": dict.fromkeys(NAMES, False),
        "muted_before": dict.fromkeys(NAMES, False),
        "time": dict.fromkeys(NAMES, NOTHING_KEPT),
        "time_before": dict.fromkeys(NAMES, NOTHING_KEPT),
        "spoken": None,
        "chat": 0,
    }
    return Window(**{**fields, **changes})


def done(skill: str, **changes: Any) -> Window:
    outcome = ActOutcome(status=ActStatus.DONE, reply="Done.", skill=skill)
    return window(outcome=outcome, chat=0, **changes)


def the_lamp_was_switched_on() -> Window:
    return done(
        "lamp",
        jobs=(job("lamp", ("homeassistant.local",), {"HA_TOKEN": HA_TOKEN}),),
        web=(call(LAMP_CALL, {"entity_id": "light.living_room"}),),
    )


class TestWhatAPlannerProposes:
    def test_a_command_is_proposed_with_its_skill_its_device_and_what_was_said(self) -> None:
        plan = proposal_for(LAMP_ON)

        assert plan["skill"] == "lamp"
        assert plan["device"] == "Living room lamp"
        assert plan["parameters"] == {"state": "on"}
        assert (plan["language"], plan["question"], plan["ask"]) == ("en", None, None)

    def test_the_values_of_the_case_are_a_copy_of_the_case(self) -> None:
        proposal_for(LAMP_ON)["parameters"]["state"] = "off"

        assert LAMP_ON.params == {"state": "on"}

    def test_what_asks_for_nothing_is_proposed_as_no_skill(self) -> None:
        plan = proposal_for(A_QUESTION)

        assert (plan["skill"], plan["device"], plan["parameters"]) == (None, None, {})

    @pytest.mark.parametrize(
        ("case", "question"),
        [
            (GARAGE_AT_THE_DISPLAY, "Vuoi davvero che apra il garage?"),
            (GARAGE_AT_THE_PHONE, "Do you really want me to close the garage door?"),
        ],
    )
    def test_where_a_yes_is_asked_for_the_question_is_in_the_language_of_the_message(
        self, case: PlanCase, question: str
    ) -> None:
        plan = proposal_for(case)

        assert plan["question"] == question
        assert plan["language"] == ("it" if case is GARAGE_AT_THE_DISPLAY else "en")

    def test_a_question_to_close_the_door_in_italian_says_so(self) -> None:
        assert question_for("close", italian=True) == "Vuoi davvero che chiuda il garage?"

    def test_a_door_that_is_refused_is_not_asked_about(self) -> None:
        assert proposal_for(GARAGE_AT_THE_KITCHEN)["question"] is None

    @pytest.mark.parametrize("case", PLAN_CASES, ids=lambda case: case.message)
    def test_what_is_proposed_for_every_case_is_a_plan_as_the_planner_reads_it(
        self, case: PlanCase
    ) -> None:
        plan = Plan.model_validate(proposal_for(case))

        assert plan.skill == case.skill
        assert plan.parameters == case.params


def examples() -> Iterator[tuple[str, Any]]:
    skills, problems = load_folder(SKILLS_FOLDER)
    assert problems == []
    yield from ((skill.manifest.name, skill) for skill in skills)


class TestWhatIsWrittenForASkillThatIsOnlyInstructions:
    def done_case(self, skill: str, **params: Any) -> PlanCase:
        return PlanCase("said", KITCHEN, skill, ActStatus.DONE, None, params)

    @pytest.mark.parametrize(
        ("skill", "values"),
        [("movie-scene", {}), ("air-quality", {"place": "Rome"})],
    )
    def test_the_plan_is_one_the_checks_accept_for_what_the_skill_declares(
        self, skill: str, values: dict[str, Any]
    ) -> None:
        written = writing_for(self.done_case(skill, **values))
        manifest = dict(examples())[skill].manifest
        assert written is not None

        check_plan(
            InstructionPlan.model_validate(written),
            manifest=manifest,
            granted=manifest.permissions,
            hosts=hosts_of(manifest.permissions, {}),
            values=values,
            device_params={},
        )

    @pytest.mark.parametrize("skill", ["movie-scene", "air-quality"])
    def test_and_it_is_written_as_strictly_as_a_model_is_asked_to_write_it(
        self, skill: str
    ) -> None:
        # What the gateway makes of an answer that does not fit the schema is no answer at all: a
        # request with no headers is that, which the stand-in wrote the first time.
        written = writing_for(self.done_case(skill, place="Rome", message="dinner is ready"))

        assert written is not None
        Draft202012Validator(RESPONSE_SCHEMA).validate(written)

    def test_a_skill_that_has_a_script_has_no_plan_written(self) -> None:
        assert writing_for(self.done_case("lamp", state="on")) is None

    def test_a_skill_of_the_platform_has_none_either_because_it_is_code_and_not_a_plan(
        self,
    ) -> None:
        assert writing_for(self.done_case("announce", message="dinner is ready")) is None

    @pytest.mark.parametrize(
        "status", [ActStatus.NEEDS_MORE, ActStatus.REFUSED, ActStatus.NEEDS_CONFIRMATION]
    )
    def test_what_is_not_going_to_be_done_has_no_plan_written(self, status: ActStatus) -> None:
        assert writing_for(PlanCase("said", KITCHEN, "announce", status)) is None

    def test_the_weather_that_cannot_be_reached_is_said_for_the_place_that_was_asked(self) -> None:
        asked = job("weather")
        asked = asked.model_copy(update={"stdin": json.dumps({"params": {"place": "Torino"}})})

        assert weather_of(asked)["message"] == "In Torino it is 21 degrees."


class TestJudgingWhatCameOfAMessage:
    def test_a_lamp_that_was_switched_on_as_asked_is_right(self) -> None:
        assert judge(Played(LAMP_ON, the_lamp_was_switched_on())) == []

    def test_what_came_to_another_status_is_said(self) -> None:
        failed = ActOutcome(status=ActStatus.FAILED, reply="The lamp skill failed.", skill="lamp")

        found = judge(Played(LAMP_ON, window(outcome=failed, chat=0)))

        assert found == ["failed instead of done"]

    def test_what_was_done_with_another_skill_is_said(self) -> None:
        outcome = ActOutcome(status=ActStatus.DONE, reply="Done.", skill="volume")

        assert judge(Played(LAMP_ON, window(outcome=outcome, chat=0))) == [
            "skill volume instead of lamp"
        ]

    def test_a_turn_that_did_not_finish_is_said(self) -> None:
        cut = TurnEnded(utterance_id="u1", status=TurnStatus.CANCELLED)

        found = judge(Played(LAMP_ON, changed(the_lamp_was_switched_on(), ended=cut)))

        assert found == ["the turn ended cancelled"]

    def test_a_message_that_the_actions_service_was_not_asked_about_once_is_said(self) -> None:
        found = judge(Played(LAMP_ON, window(asked=2, outcome=None)))

        assert found == ["the actions service was asked about it 2 times"]

    def test_a_lamp_that_was_given_another_entity_is_said(self) -> None:
        wrong = done(
            "lamp",
            jobs=(job("lamp", ("homeassistant.local",), {"HA_TOKEN": HA_TOKEN}),),
            web=(call(LAMP_CALL, {"entity_id": "light.bedroom"}),),
        )

        [problem] = judge(Played(LAMP_ON, wrong))

        assert "light.bedroom" in problem

    def test_a_lamp_that_was_asked_without_the_token_of_the_home_is_said(self) -> None:
        wrong = done(
            "lamp",
            jobs=(job("lamp", ("homeassistant.local",), {"HA_TOKEN": HA_TOKEN}),),
            web=(call(LAMP_CALL, {"entity_id": "light.living_room"}, token="another"),),
        )

        assert judge(Played(LAMP_ON, wrong)) == [
            "Home Assistant was not given the token of the home"
        ]

    def test_a_script_that_could_reach_more_than_it_declared_or_was_given_more_is_said(
        self,
    ) -> None:
        wrong = done(
            "lamp",
            jobs=(
                job(
                    "lamp",
                    ("homeassistant.local", "elsewhere.example"),
                    {"HA_TOKEN": HA_TOKEN, "X": "y"},
                ),
            ),
            web=(call(LAMP_CALL, {"entity_id": "light.living_room"}),),
        )

        found = judge(Played(LAMP_ON, wrong))

        assert len(found) == 2
        assert "elsewhere.example" in found[0]
        assert "'X'" in found[1]

    def test_a_volume_that_was_set_on_the_device_that_was_spoken_to_is_right(self) -> None:
        events = dict.fromkeys(NAMES, ()) | {KITCHEN: ("volume 30",)}
        volumes = dict.fromkeys(NAMES, 50) | {KITCHEN: 30}
        set_to_30 = done("volume", events=events, volumes=volumes)

        assert judge(Played(VOLUME, set_to_30)) == []

    def test_a_volume_that_a_device_was_told_and_did_not_take_is_said(self) -> None:
        events = dict.fromkeys(NAMES, ()) | {KITCHEN: ("volume 30",)}
        stuck = done("volume", events=events)  # still at 50

        assert judge(Played(VOLUME, stuck)) == [f"{KITCHEN} has its volume at 50"]

    def test_a_volume_that_was_set_to_another_level_or_on_another_device_is_said(self) -> None:
        events = dict.fromkeys(NAMES, ()) | {DISPLAY: ("volume 30",)}

        found = judge(Played(VOLUME, done("volume", events=events)))

        assert found[0].startswith(f"{KITCHEN} was told")

    @staticmethod
    def turned(case: PlanCase, *, before: int, now: int, told: str | None = None) -> Window:
        """A volume that went from ``before`` to ``now`` on the kitchen speaker."""
        events = dict.fromkeys(NAMES, ()) | {KITCHEN: (told or f"volume {now}",)}
        return done(
            "volume-change",
            events=events,
            volumes=dict.fromkeys(NAMES, 50) | {KITCHEN: now},
            volumes_before=dict.fromkeys(NAMES, 50) | {KITCHEN: before},
        )

    def test_a_volume_turned_up_goes_by_the_step_of_the_skill_when_nothing_was_said(self) -> None:
        assert judge(Played(TURNED_UP, self.turned(TURNED_UP, before=50, now=60))) == []

    def test_a_volume_turned_down_goes_by_what_was_said(self) -> None:
        turned = self.turned(TURNED_DOWN_BY_TWENTY, before=50, now=30)

        assert judge(Played(TURNED_DOWN_BY_TWENTY, turned)) == []

    def test_it_goes_from_where_the_device_had_it_and_never_past_either_end(self) -> None:
        assert judge(Played(TURNED_UP, self.turned(TURNED_UP, before=95, now=100))) == []
        low = self.turned(TURNED_DOWN_BY_TWENTY, before=15, now=0)
        assert judge(Played(TURNED_DOWN_BY_TWENTY, low)) == []

    def test_a_volume_that_went_by_another_step_is_said(self) -> None:
        found = judge(Played(TURNED_UP, self.turned(TURNED_UP, before=50, now=70)))

        assert found == [
            f"{KITCHEN} was told ('volume 70',) instead of 60",
            f"{KITCHEN} has its volume at 70",
        ]

    def test_a_device_that_was_told_and_did_not_take_it_is_said(self) -> None:
        stuck = self.turned(TURNED_UP, before=50, now=50, told="volume 60")

        assert judge(Played(TURNED_UP, stuck)) == [f"{KITCHEN} has its volume at 50"]

    def test_another_device_whose_volume_moved_is_said(self) -> None:
        moved = changed(
            self.turned(TURNED_UP, before=50, now=60),
            volumes=dict.fromkeys(NAMES, 50) | {KITCHEN: 60, DISPLAY: 60},
        )

        assert judge(Played(TURNED_UP, moved)) == [f"{DISPLAY} had its volume moved to 60"]

    def test_a_skill_of_the_platform_runs_nothing_in_a_sandbox(self) -> None:
        ran = changed(self.turned(TURNED_UP, before=50, now=60), jobs=(job("volume-change"),))

        assert judge(Played(TURNED_UP, ran)) == ["it ran ['volume-change']"]

    @staticmethod
    def sound(
        *,
        muted: bool,
        was_muted: bool = False,
        told: tuple[str, ...] | None = None,
        reply: str = "Done.",
    ) -> Window:
        """The kitchen speaker's sound muted or brought back, from the state it was in."""
        events = dict.fromkeys(NAMES, ()) | {
            KITCHEN: told or (("muted",) if muted else ("unmuted",))
        }
        return window(
            outcome=ActOutcome(status=ActStatus.DONE, reply=reply, skill="mute"),
            chat=0,
            events=events,
            muted=dict.fromkeys(NAMES, False) | {KITCHEN: muted},
            muted_before=dict.fromkeys(NAMES, False) | {KITCHEN: was_muted},
        )

    def test_a_sound_that_was_muted_on_the_device_that_was_spoken_to_is_right(self) -> None:
        assert judge(Played(MUTED, self.sound(muted=True))) == []
        assert judge(Played(UNMUTED, self.sound(muted=False, was_muted=True))) == []

    def test_a_sound_that_was_told_to_mute_and_was_not_is_said(self) -> None:
        stuck = changed(self.sound(muted=True), muted=dict.fromkeys(NAMES, False))
        brought_back = changed(
            self.sound(muted=False, was_muted=True),
            muted=dict.fromkeys(NAMES, False) | {KITCHEN: True},
        )

        assert judge(Played(MUTED, stuck)) == [f"{KITCHEN} has its sound on"]
        assert judge(Played(UNMUTED, brought_back)) == [f"{KITCHEN} has its sound muted"]

    def test_a_device_that_was_told_something_else_is_said(self) -> None:
        wrong = self.sound(muted=True, told=("volume 0",))

        assert judge(Played(MUTED, wrong)) == [f"{KITCHEN} was told ('volume 0',)"]

    def test_muting_that_lost_the_level_is_said(self) -> None:
        lost = changed(self.sound(muted=True), volumes=dict.fromkeys(NAMES, 50) | {KITCHEN: 0})

        assert judge(Played(MUTED, lost)) == [f"{KITCHEN} lost the level of its volume"]

    def test_another_device_whose_sound_changed_is_said(self) -> None:
        other = changed(
            self.sound(muted=True),
            muted=dict.fromkeys(NAMES, False) | {KITCHEN: True, DISPLAY: True},
        )

        assert judge(Played(MUTED, other)) == [f"{DISPLAY} had its sound changed"]

    def test_a_skill_of_the_platform_that_ran_in_a_sandbox_is_said_for_a_mute_too(self) -> None:
        ran = changed(self.sound(muted=True), jobs=(job("mute"),))

        assert judge(Played(MUTED, ran)) == ["it ran ['mute']"]

    @staticmethod
    def said_how_loud(reply: str, *, muted: bool = False, level: int = 50) -> Window:
        return window(
            outcome=ActOutcome(status=ActStatus.DONE, reply=reply, skill="volume-status"),
            chat=0,
            volumes=dict.fromkeys(NAMES, 50) | {KITCHEN: level},
            volumes_before=dict.fromkeys(NAMES, 50) | {KITCHEN: level},
            muted=dict.fromkeys(NAMES, False) | {KITCHEN: muted},
            muted_before=dict.fromkeys(NAMES, False) | {KITCHEN: muted},
        )

    def test_a_question_to_the_device_is_right_when_it_is_said_what_the_device_has(self) -> None:
        assert judge(Played(HOW_LOUD, self.said_how_loud("The volume is at 50 percent."))) == []
        muted = self.said_how_loud(
            "L'audio è in muto. Il volume è al 35 per cento.", muted=True, level=35
        )
        assert judge(Played(HOW_LOUD, muted)) == []

    def test_a_level_that_is_not_the_one_the_device_has_is_said(self) -> None:
        wrong = self.said_how_loud("The volume is at 80 percent.", level=35)

        assert judge(Played(HOW_LOUD, wrong)) == [
            "the volume is 35 and it was said 'The volume is at 80 percent.'"
        ]

    def test_a_sound_that_is_muted_is_said_to_be_and_one_that_is_not_is_not(self) -> None:
        not_said = self.said_how_loud("The volume is at 50 percent.", muted=True)
        said_wrongly = self.said_how_loud("The sound is muted. The volume is at 50 percent.")

        assert judge(Played(HOW_LOUD, not_said)) == [
            "the sound is muted and it was said 'The volume is at 50 percent.'"
        ]
        assert judge(Played(HOW_LOUD, said_wrongly)) == [
            "the sound is not muted and it was said "
            "'The sound is muted. The volume is at 50 percent.'"
        ]

    def test_a_question_that_changed_something_is_said(self) -> None:
        moved = changed(
            self.said_how_loud("The volume is at 50 percent."),
            events=dict.fromkeys(NAMES, ()) | {KITCHEN: ("volume 60",)},
            volumes=dict.fromkeys(NAMES, 50) | {KITCHEN: 60},
        )

        found = judge(Played(HOW_LOUD, moved))

        assert any(line.startswith("a question changed something") for line in found)
        assert "a question changed the sound of a device" in found

    def test_what_no_skill_fits_is_right_when_nothing_ran_and_no_model_was_asked(self) -> None:
        assert judge(Played(A_QUESTION, window())) == []

    def test_a_model_that_was_asked_to_answer_it_is_said(self) -> None:
        assert judge(Played(A_QUESTION, window(chat=1))) == ["the chat model wrote 1 answers"]

    def test_it_is_told_so_in_the_sentence_of_the_code_in_the_language_of_the_message(self) -> None:
        english = window(
            spoken={KITCHEN: ("I found no skill that fits, and no relevant document.",)}
        )
        italian = PlanCase("che ore sono", KITCHEN, None, ActStatus.NO_SKILL)
        in_italian = window(
            spoken={KITCHEN: ("Non ho trovato una skill adatta, né documenti pertinenti.",)}
        )

        assert judge(Played(A_QUESTION, english)) == []
        assert judge(Played(italian, in_italian)) == []

    def test_another_sentence_is_said_with_what_it_should_have_been(self) -> None:
        wrong = window(spoken={KITCHEN: ("I do not know.",)})

        [problem] = judge(Played(A_QUESTION, wrong))

        assert problem == (
            "it said 'I do not know.' instead of "
            "'I found no skill that fits, and no relevant document.'"
        )

    def test_the_wrong_language_is_another_sentence(self) -> None:
        italian = PlanCase("che ore sono", KITCHEN, None, ActStatus.NO_SKILL)
        in_english = window(
            spoken={KITCHEN: ("I found no skill that fits, and no relevant document.",)}
        )

        [problem] = judge(Played(italian, in_english))

        assert problem.endswith("'Non ho trovato una skill adatta, né documenti pertinenti.'")

    def test_what_was_not_heard_is_not_looked_for(self) -> None:
        assert judge(Played(A_QUESTION, window(spoken=None))) == []

    def test_a_question_that_ran_something_is_said(self) -> None:
        found = judge(Played(A_QUESTION, window(jobs=(job("lamp"),))))

        assert found == ["it ran ['lamp']"]

    def test_what_asks_for_more_is_right_when_nothing_ran_and_the_code_says_what_is_missing(
        self,
    ) -> None:
        asked = ActOutcome(
            status=ActStatus.NEEDS_MORE,
            reply="What level?",
            skill="volume",
            code=ActCode.MISSING_VALUES,
        )

        assert judge(Played(TOO_LITTLE, window(outcome=asked, chat=0))) == []

    def test_what_asks_for_more_for_another_reason_is_said(self) -> None:
        asked = ActOutcome(
            status=ActStatus.NEEDS_MORE, reply="Which?", skill="volume", code=ActCode.NO_DEVICE
        )

        assert judge(Played(TOO_LITTLE, window(outcome=asked, chat=0))) == [
            "code no_device instead of missing_values"
        ]

    def test_a_door_that_is_refused_to_a_speaker_is_right_when_nothing_moved(self) -> None:
        refused = ActOutcome(
            status=ActStatus.REFUSED, reply="No.", skill="garage-door", code=ActCode.DENIED
        )

        assert judge(Played(GARAGE_AT_THE_KITCHEN, window(outcome=refused, chat=0))) == []

    def test_a_door_that_was_refused_and_moved_is_said(self) -> None:
        refused = ActOutcome(
            status=ActStatus.REFUSED, reply="No.", skill="garage-door", code=ActCode.DENIED
        )
        moved = window(outcome=refused, chat=0, web=(call(LAMP_CALL, {}),))

        [problem] = judge(Played(GARAGE_AT_THE_KITCHEN, moved))

        assert problem.startswith("it reached")

    def test_a_refusal_for_another_reason_is_said(self) -> None:
        refused = ActOutcome(
            status=ActStatus.REFUSED,
            reply="No.",
            skill="garage-door",
            code=ActCode.APPROVAL_UNAVAILABLE,
        )

        assert judge(Played(GARAGE_AT_THE_KITCHEN, window(outcome=refused, chat=0))) == [
            "code approval_unavailable instead of denied"
        ]

    def test_something_that_was_announced_where_nothing_should_have_been_is_said(self) -> None:
        said = dict.fromkeys(NAMES, ()) | {DISPLAY: ("hello",)}

        found = judge(Played(A_QUESTION, window(announced=said)))

        assert found == [f"{DISPLAY} was told to say ['hello']"]


DINNER = PlanCase(
    "tell everyone that dinner is ready",
    KITCHEN,
    "announce",
    ActStatus.DONE,
    None,
    {"message": "dinner is ready"},
)
DINNER_AT_THE_DISPLAY = PlanCase(
    "announce on the living room display that dinner is ready",
    KITCHEN,
    "announce",
    ActStatus.DONE,
    "Living room display",
    {"message": "dinner is ready"},
)


def announced(message: str, speaker: str, target: str, **changes: Any) -> Window:
    """What the series sees of an announcement that went as it should: the speaker says "Done."
    where it was spoken to and the target says the message (both, on the same device)."""
    heard: dict[str, list[str]] = {name: [] for name in NAMES}
    heard[speaker].append("Done.")
    heard[target].append(message)
    fields: dict[str, Any] = {
        "sentences": {name: len(words) for name, words in heard.items()},
        "spoken": {name: tuple(words) for name, words in heard.items()},
    }
    return done("announce", **{**fields, **changes})


class TestJudgingAnAnnouncement:
    def test_a_message_said_on_the_device_that_was_spoken_to_is_right(self) -> None:
        window_ = announced("dinner is ready", KITCHEN, KITCHEN)

        assert judge(Played(DINNER, window_)) == []

    def test_so_is_one_said_on_the_device_that_was_named_while_the_answer_goes_to_the_speaker(
        self,
    ) -> None:
        window_ = announced("dinner is ready", KITCHEN, DISPLAY)

        assert judge(Played(DINNER_AT_THE_DISPLAY, window_)) == []

    def test_the_order_that_the_two_are_said_in_is_not_what_is_judged(self) -> None:
        window_ = announced(
            "dinner is ready",
            KITCHEN,
            KITCHEN,
            spoken={KITCHEN: ("Done.", "dinner is ready"), DISPLAY: (), PHONE: ()},
        )

        assert judge(Played(DINNER, window_)) == []

    def test_where_the_sound_cannot_be_read_back_it_is_the_number_of_sentences_that_is(
        self,
    ) -> None:
        window_ = announced("dinner is ready", KITCHEN, DISPLAY, spoken=None)

        assert judge(Played(DINNER_AT_THE_DISPLAY, window_)) == []

    def test_a_message_that_was_said_nowhere_is_said(self) -> None:
        silent = announced("dinner is ready", KITCHEN, KITCHEN, sentences=dict.fromkeys(NAMES, 0))

        [problem] = judge(Played(DINNER, silent))

        assert "sentences were said instead of" in problem

    def test_a_message_said_on_another_device_than_the_one_it_was_for_is_said(self) -> None:
        wrong = announced("dinner is ready", KITCHEN, PHONE)

        counted, words = judge(Played(DINNER_AT_THE_DISPLAY, wrong))

        assert "sentences were said instead of" in counted
        assert "was said instead of" in words

    def test_other_words_than_the_message_are_said(self) -> None:
        wrong = announced(
            "dinner is ready",
            KITCHEN,
            KITCHEN,
            spoken={KITCHEN: ("Done.", "lunch is ready"), DISPLAY: (), PHONE: ()},
        )

        [problem] = judge(Played(DINNER, wrong))

        assert "lunch is ready" in problem

    def test_what_was_run_in_a_sandbox_for_it_is_said(self) -> None:
        ran = announced("dinner is ready", KITCHEN, KITCHEN, jobs=(job("announce"),))

        assert judge(Played(DINNER, ran)) == ["it ran ['announce']"]

    def test_what_it_reached_on_the_network_is_said(self) -> None:
        reached = announced(
            "dinner is ready", KITCHEN, KITCHEN, web=(call(LAMP_CALL, {"entity_id": "x"}),)
        )

        [problem] = judge(Played(DINNER, reached))

        assert problem.startswith("it reached")

    def test_words_that_a_device_was_told_to_speak_are_said_for_an_announcement_is_a_clip(
        self,
    ) -> None:
        told = dict.fromkeys(NAMES, ()) | {KITCHEN: ("dinner is ready",)}

        [problem] = judge(
            Played(DINNER, announced("dinner is ready", KITCHEN, KITCHEN, announced=told))
        )

        assert "was told to speak words" in problem


def questioned(**changes: Any) -> Window:
    outcome = ActOutcome(status=ActStatus.NEEDS_CONFIRMATION, reply="Really?", skill="garage-door")
    return window(outcome=outcome, chat=0, **changes)


def answered(status: ActStatus, **changes: Any) -> Window:
    outcome = ActOutcome(status=status, reply="Okay.", skill="garage-door")
    return window(outcome=outcome, chat=0, **changes)


class TestJudgingAQuestionThatIsAsked:
    def test_a_yes_at_the_display_that_opened_the_door_is_right(self) -> None:
        opened = answered(
            ActStatus.DONE,
            jobs=(job("garage-door", ("homeassistant.local",), {"HA_TOKEN": HA_TOKEN}),),
            web=(
                call(
                    "http://homeassistant.local:8123/api/services/cover/open_cover",
                    {"entity_id": ENTITIES["Garage door"]},
                ),
            ),
        )

        played = Played(GARAGE_AT_THE_DISPLAY, questioned(), opened, "Sì.")

        assert judge(played) == []

    def test_a_yes_that_did_not_open_the_door_is_said(self) -> None:
        nothing = answered(ActStatus.CANCELLED)

        found = judge(Played(GARAGE_AT_THE_DISPLAY, questioned(), nothing, "Sì."))

        assert found == ["a yes was cancelled"]

    def test_a_no_at_the_phone_that_left_the_door_shut_is_right(self) -> None:
        shut = answered(ActStatus.CANCELLED)

        assert judge(Played(GARAGE_AT_THE_PHONE, questioned(), shut, "No.")) == []

    def test_a_no_that_opened_the_door_is_said(self) -> None:
        opened = answered(ActStatus.DONE, jobs=(job("garage-door"),))

        found = judge(Played(GARAGE_AT_THE_PHONE, questioned(), opened, "No."))

        assert found[0] == "a no was done"
        assert found[1] == "it ran ['garage-door']"

    def test_a_door_that_moved_before_the_answer_is_said(self) -> None:
        early = questioned(jobs=(job("garage-door"),))
        shut = answered(ActStatus.CANCELLED)

        found = judge(Played(GARAGE_AT_THE_PHONE, early, shut, "No."))

        assert found == ["it ran ['garage-door']"]

    def test_a_question_that_was_not_answered_is_said(self) -> None:
        found = judge(Played(GARAGE_AT_THE_PHONE, questioned()))

        assert found == ["what was asked was not answered"]

    def test_a_yes_is_said_where_a_device_may_open_it_and_a_no_where_a_phone_is_asked(self) -> None:
        assert follows_with_a_yes(GARAGE_AT_THE_DISPLAY)
        assert not follows_with_a_yes(GARAGE_AT_THE_PHONE)
        assert (YES["it"], YES["en"], NO["it"], NO["en"]) == ("Sì.", "Yes.", "No.", "No.")

    def test_a_series_is_a_list_of_lines_one_for_each_case_that_is_wrong(self) -> None:
        wrong = Played(LAMP_ON, window(outcome=None, asked=0))
        right = Played(A_QUESTION, window())

        lines = problems_of([right, wrong])

        assert lines == [
            "'turn on the living room lamp' at Kitchen speaker: "
            "the actions service was asked about it 0 times"
        ]


def record(
    skill: str, decision: DecisionOutcome, result: ActionResult, confirmed: bool | None
) -> AuditRecord:
    tenant = make_tenant_id()
    return AuditRecord(
        id=AuditId(uuid4()),
        tenant_id=tenant,
        occurred_at=datetime.now(UTC),
        acts_as=Subject.tenant(tenant),
        skill_id=SkillId(uuid4()),
        skill_name=skill,
        decision=decision,
        result=result,
        confirmed=confirmed,
    )


class TestWhatTheTrailMustHold:
    def played(self) -> list[Played]:
        return [
            Played(LAMP_ON, window()),
            Played(GARAGE_AT_THE_KITCHEN, window()),
            Played(GARAGE_AT_THE_DISPLAY, window(), window(), "Sì."),
            Played(GARAGE_AT_THE_PHONE, window(), window(), "No."),
            Played(TOO_LITTLE, window()),
            Played(A_QUESTION, window()),
            Played(
                PlanCase("riavvia il router", DISPLAY, "restart-router", ActStatus.REFUSED),
                window(),
            ),
        ]

    def test_what_was_done_refused_and_asked_about_is_in_it_in_the_order_it_was_said(self) -> None:
        asked_about = DecisionOutcome.REQUIRE_CONFIRMATION

        assert trail_of(self.played()) == [
            ("lamp", DecisionOutcome.ALLOW, ActionResult.EXECUTED, None),
            ("garage-door", DecisionOutcome.DENY, ActionResult.NOT_EXECUTED, None),
            ("garage-door", asked_about, ActionResult.NOT_EXECUTED, None),
            ("garage-door", asked_about, ActionResult.EXECUTED, True),  # the yes
            ("garage-door", asked_about, ActionResult.NOT_EXECUTED, None),
            ("garage-door", asked_about, ActionResult.NOT_EXECUTED, False),  # the no
            ("restart-router", DecisionOutcome.REQUIRE_APPROVAL, ActionResult.NOT_EXECUTED, None),
        ]

    def test_the_trail_of_the_service_is_read_the_oldest_first(self) -> None:
        latest_first = (
            record("volume", DecisionOutcome.ALLOW, ActionResult.EXECUTED, None),
            record("lamp", DecisionOutcome.DENY, ActionResult.NOT_EXECUTED, None),
        )

        assert as_steps(latest_first) == [
            ("lamp", DecisionOutcome.DENY, ActionResult.NOT_EXECUTED, None),
            ("volume", DecisionOutcome.ALLOW, ActionResult.EXECUTED, None),
        ]


class TestWhatWasHeard:
    def test_it_is_compared_without_accents_capitals_or_punctuation(self) -> None:
        assert plain("Sì, apri il Garage!") == "si apri il garage"
        assert likeness("Si", "Sì.") == 1.0

    def test_what_is_alike_is_near_and_what_is_not_is_far(self) -> None:
        assert likeness("accendi la luce", "accendi la luce del salotto") > 0.7
        assert likeness("apri il garage", "spegni la lampada") < 0.5
        assert likeness("", "apri il garage") == 0.0

    def test_what_was_heard_is_taken_for_the_message_it_is_nearest_to(self) -> None:
        messages = ["accendi la luce del salotto", "spegni la lampada", "apri il garage"]

        assert nearest("accendi la luche del salotto", messages) == messages[0]
        assert nearest("spegni la lanpada", messages) == messages[1]

    def test_it_is_a_line_for_each_case_with_how_alike(self) -> None:
        heard = window(heard="accendi la luce del salotto")
        said = PlanCase("accendi la luce del salotto", KITCHEN, "lamp", ActStatus.DONE)

        [line] = table_of([Played(said, heard)])

        assert line == "1.00  'accendi la luce del salotto' heard as 'accendi la luce del salotto'"

    def test_what_was_not_heard_is_a_line_too(self) -> None:
        said = PlanCase("apri il garage", KITCHEN, "garage-door", ActStatus.REFUSED)

        [line] = table_of([Played(said, window(heard=None))])

        assert line == "0.00  'apri il garage' heard as None"


class TestTheRunnerOfTheHouse:
    def test_a_skill_that_cannot_run_here_prints_what_it_was_told_to_and_is_not_run(self) -> None:
        runner = HouseRunner()
        runner.canned["weather"] = weather_of
        asked = job("weather").model_copy(
            update={"stdin": json.dumps({"params": {"place": "Rome"}})}
        )

        result = handled(runner, asked)

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "message": "In Rome it is 21 degrees.",
            "data": {"temperature_2m": 21},
        }

    def test_a_skill_that_can_is_run_as_it_is(self) -> None:
        runner = HouseRunner()
        script = JobFile.of("run.py", b'print(\'{"message": "ran"}\')')
        asked = Job(label="lamp", files=(script,), entrypoint="run.py", timeout_s=10)

        result = runner.run(asked)

        assert (result.exit_code, json.loads(result.stdout)) == (0, {"message": "ran"})

    def test_the_jobs_are_kept_whether_they_were_run_or_not(self) -> None:
        runner = HouseRunner()
        runner.canned["weather"] = lambda _job: {"message": "canned"}
        asked = job("weather")

        handled(runner, asked)

        assert runner.jobs == [asked]


class TestTheStandInOfTheModel:
    async def test_what_it_was_told_to_propose_is_what_the_planner_gets(self) -> None:
        model = HouseholdModel()
        model.proposal = proposal_for(LAMP_ON)

        text = await ask(model, "Message: whatever was heard", {"skill": {}, "reason": {}})

        assert json.loads(text)["skill"] == "lamp"
        assert model.plans == ["Message: whatever was heard"]

    async def test_without_being_told_it_reads_the_message_as_it_always_did(self) -> None:
        model = HouseholdModel()

        text = await ask(model, "Message: set the volume to 20", {"skill": {}, "reason": {}})

        assert json.loads(text) == plan_for("Message: set the volume to 20")

    async def test_what_it_was_told_to_write_is_what_the_writer_of_plans_gets(self) -> None:
        model = HouseholdModel()
        model.writing = writing_for(PlanCase("said", KITCHEN, "movie-scene", ActStatus.DONE))

        text = await ask(model, "the instructions", {"reason": {}, "steps": {}, "say": {}})

        assert json.loads(text)["steps"][0]["id"] == "ceiling"
        assert model.writings == ["the instructions"]

    async def test_asked_to_write_what_it_was_not_told_to_it_says_so_and_does_not_make_one_up(
        self,
    ) -> None:
        model = HouseholdModel()

        with pytest.raises(AssertionError, match="not told which"):
            await ask(model, "the instructions", {"reason": {}, "steps": {}, "say": {}})


async def ask(model: HouseholdModel, shown: str, properties: dict[str, Any]) -> str:
    request = ChatRequest(
        role=ModelRole.CHAT_FAST,
        messages=(ChatMessage(role=MessageRole.USER, content=shown),),
        response_schema={"type": "object", "properties": properties},
    )
    result = await model.chat(cast(Any, None), cast(Any, None), request)
    return result.text


class TestACase:
    """What a series does around a message, with devices that are only what it looks at of them."""

    def a_house(self) -> House:
        return cast(
            House,
            SimpleNamespace(
                model=HouseholdModel(),
                runner=HouseRunner(),
                acts=[],
                home_assistant=SimpleNamespace(seen=[]),
            ),
        )

    def a_device(self) -> Any:
        shown = RecordingRenderer()
        return SimpleNamespace(
            shown=shown,
            timekeeper=Timekeeper(shown, clock=FakeClock()),
            hardware=SimpleNamespace(state=SimpleNamespace(spoken=[], volume=50, muted=False)),
            client=SimpleNamespace(wait_until_played=lambda: asyncio.sleep(0)),
            timeout_s=1.0,
            sentences=list,
            understood=lambda: ["words"],
            spoken=list,
        )

    async def test_the_planner_is_told_what_to_propose_for_the_message_and_not_for_the_next(
        self,
    ) -> None:
        house = self.a_house()
        proposed: list[Any] = []

        async def say(device: Any, words: str) -> TurnEnded:
            proposed.append(house.model.proposal)
            return ENDED

        series = Series(house, {KITCHEN: self.a_device()}, say)

        await series.play(A_QUESTION)

        assert proposed == [proposal_for(A_QUESTION)]
        assert house.model.proposal is None
        assert house.model.writing is None

    async def test_a_case_that_goes_wrong_leaves_no_proposal_behind(self) -> None:
        house = self.a_house()

        async def say(device: Any, words: str) -> TurnEnded:
            raise TimeoutError

        series = Series(house, {KITCHEN: self.a_device()}, say)

        with pytest.raises(asyncio.TimeoutError):
            await series.play(A_QUESTION)

        assert house.model.proposal is None

    async def test_what_every_device_was_given_to_say_is_played_before_what_came_of_it_is_read(
        self,
    ) -> None:
        # A skill may have another device say something while the answer is given on this one: a
        # window read before that device had finished would be one in which it had said less.
        house = self.a_house()
        events: list[str] = []

        def device_of(name: str) -> Any:
            device = self.a_device()

            async def wait() -> None:
                events.append(f"waited for {name}")

            device.client = SimpleNamespace(wait_until_played=wait)
            return device

        async def say(device: Any, words: str) -> TurnEnded:
            events.append("said")
            return ENDED

        devices = {name: device_of(name) for name in NAMES}
        series = Series(house, devices, say)

        await series.play(A_QUESTION)

        assert events == ["said", *(f"waited for {name}" for name in NAMES)]

    async def test_the_skill_that_cannot_run_here_is_told_what_it_prints(self) -> None:
        house = self.a_house()

        Series(house, {}, lambda device, words: asyncio.sleep(0, ENDED))

        assert "weather" in house.runner.canned

    async def test_a_question_is_followed_by_a_yes_or_a_no_in_the_language_it_was_asked_in(
        self,
    ) -> None:
        house = self.a_house()
        said: list[str] = []

        async def say(device: Any, words: str) -> TurnEnded:
            said.append(words)
            return ENDED

        devices = {DISPLAY: self.a_device(), PHONE: self.a_device()}
        series = Series(house, devices, say)

        played = [await series.play(GARAGE_AT_THE_DISPLAY), await series.play(GARAGE_AT_THE_PHONE)]

        assert said == ["apri il garage", "Sì.", "close the garage", "No."]
        assert [each.answered_with for each in played] == ["Sì.", "No."]
        assert house.home_assistant.seen == []
