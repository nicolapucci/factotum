"""How a planner is measured against the corpus: what is wrong with what became of a message. A live
test measures a real model with this, and a harness that cannot give a perfect score to a planner
that is told the right answer is one that measures itself (it took every skill of the platform for
"nothing was run" while only the runner was looked at)."""

from pathlib import Path
from typing import Any

import pytest

from factotum.actions.core import platform_skills
from factotum.actions.measure import ENTITIES, Judged, judge, measure, same, same_value
from factotum.actions.plan_cases import DISPLAY, KITCHEN, PlanCase
from factotum.actions.rig import HOME_TEXT, ActionsRig, plan_of
from factotum.actions.skills import load_folder
from factotum.actions.testing import FakeSpeech, a_device_answers
from factotum.contracts.actions import ActOutcome, ActStatus
from factotum.contracts.skills import ParamValue

EXAMPLES = Path(__file__).resolve().parents[3] / "skills" / "examples"

LAMP_ON = PlanCase(
    "turn on the living room lamp",
    KITCHEN,
    "lamp",
    ActStatus.DONE,
    "Living room lamp",
    {"state": "on"},
)
VOLUME = PlanCase("set the volume to 30", KITCHEN, "volume", ActStatus.DONE, None, {"level": 30})
QUESTION = PlanCase("tell me a joke", KITCHEN, None, ActStatus.NO_SKILL)
ANNOUNCE = PlanCase(
    "tell everyone that dinner is ready",
    KITCHEN,
    "announce",
    ActStatus.DONE,
    None,
    {"message": "dinner is ready"},
)


def done(skill: str | None, status: ActStatus = ActStatus.DONE) -> ActOutcome:
    if skill is None:
        return ActOutcome.no_skill()
    return ActOutcome(status=status, reply="As told.", skill=skill)


class TestSameWords:
    @pytest.mark.parametrize(
        ("understood", "wanted"),
        [
            ("Rome", "rome"),
            ("rome.", "Rome"),
            ("  Rome?! ", "Rome"),
            ("dinner is ready", "Dinner is ready."),
            (30, 30),
            (True, True),
        ],
    )
    def test_a_text_is_the_same_whatever_its_case_and_its_last_punctuation(
        self, understood: object, wanted: ParamValue
    ) -> None:
        assert same(understood, wanted)

    @pytest.mark.parametrize(
        ("understood", "wanted"),
        [
            ("Rome", "Milan"),
            ("the dinner is ready", "dinner is ready"),
            (30, 40),
            ("30", 30),
            (None, "Rome"),
            (None, 0),
            (False, True),
        ],
    )
    def test_anything_else_is_not(self, understood: object, wanted: ParamValue) -> None:
        assert not same(understood, wanted)


class TestSameValue:
    @pytest.mark.parametrize(
        ("skill", "name", "understood", "wanted"),
        [
            ("alarm-change", "time", "alle otto", "otto"),
            ("alarm-change", "time", "7:30", "sette e mezza"),
            ("alarm-change", "from", "delle sette", "sette"),
            ("alarm-change", "from", "seven o'clock", "seven"),
            ("alarm", "time", "8am", "otto"),
            ("alarm-cancel", "time", "sette", "alle sette"),
            ("timer", "duration", "ten minutes", "ten minute"),
            ("timer", "duration", "10 minutes", "ten minutes"),
            ("timer-cancel", "duration", "cinque minuti", "five minutes"),
            ("alarm-postpone", "by", "half an hour", "mezz'ora"),
            ("alarm", "day", "tomorrow", "domani"),
            ("alarm-change", "day", "Monday", "lunedì"),
        ],
    )
    def test_a_value_that_code_reads_is_the_same_when_it_is_read_the_same(
        self, skill: str, name: str, understood: str, wanted: str
    ) -> None:
        assert same_value(skill, name, understood, wanted)

    @pytest.mark.parametrize(
        ("skill", "name", "understood", "wanted"),
        [
            ("alarm-change", "time", "alle nove", "otto"),
            ("alarm-change", "time", "8:30", "otto"),
            ("alarm-change", "from", "sette", "otto"),
            ("timer", "duration", "ten minutes", "five minutes"),
            ("timer", "duration", "ten seconds", "ten minutes"),
            ("alarm-postpone", "by", "sveglia", "ten minutes"),
            ("alarm", "day", "tomorrow", "lunedì"),
            ("alarm", "day", "sveglia", "domani"),
        ],
    )
    def test_a_value_that_is_read_another_way_or_not_at_all_is_not(
        self, skill: str, name: str, understood: str, wanted: str
    ) -> None:
        assert not same_value(skill, name, understood, wanted)

    def test_a_value_that_is_not_read_by_code_is_the_same_by_its_words(self) -> None:
        assert same_value("announce", "message", "Dinner is ready.", "dinner is ready")
        assert not same_value("announce", "message", "dinner", "dinner is ready")
        assert same_value("weather", "place", "rome", "Rome")

    @pytest.mark.parametrize(
        ("understood", "wanted"),
        [
            ("to call the dentist", "call the dentist"),
            ("To call the dentist.", "call the dentist"),
            ("di comprare il pane", "comprare il pane"),
            ("that dinner is ready", "dinner is ready"),
            ("che arrivo tra dieci minuti", "arrivo tra dieci minuti"),
            ("una riunione con Marco", "riunione con Marco"),
            ("il pranzo con Luca", "pranzo con Luca"),
            ("a meeting with Marco", "meeting with Marco"),
            ("The dentist appointment", "dentist appointment"),
            ("riunione con Marco", "una riunione con Marco"),
            ("una", "una"),
        ],
    )
    def test_the_words_that_were_said_with_the_one_that_introduces_them_are_the_same_words(
        self, understood: str, wanted: str
    ) -> None:
        assert same_value("reminder-add", "what", understood, wanted)

    @pytest.mark.parametrize(
        ("understood", "wanted"),
        [
            ("call to the dentist", "call the dentist"),
            ("to", "call the dentist"),
            ("to call the vet", "call the dentist"),
            ("so call the dentist", "call the dentist"),
            ("then the call the dentist", "call the dentist"),
            ("una una riunione", "riunione"),
            ("la riunione con Anna", "riunione con Marco"),
            ("una", "riunione"),
            ("to", "di"),  # a word that is all there is, is a word
        ],
    )
    def test_only_that_one_word_in_front_is_let_through(self, understood: str, wanted: str) -> None:
        assert not same_value("reminder-add", "what", understood, wanted)

    def test_a_value_that_is_missing_or_is_not_a_text_is_compared_as_it_is(self) -> None:
        assert not same_value("timer", "duration", None, "ten minutes")
        assert not same_value("timer", "duration", 600, "ten minutes")
        assert same_value("volume", "level", 30, 30)
        assert not same_value("volume", "level", 30, 40)

    def test_a_case_that_cannot_be_read_falls_back_to_its_words(self) -> None:
        assert same_value("timer", "duration", "soon", "soon")
        assert not same_value("timer", "duration", "soon", "later")


class TestJudging:
    def test_a_message_that_is_read_as_the_corpus_says_has_nothing_wrong(self) -> None:
        assert judge(VOLUME, done("volume"), {"level": 30}) == []

    def test_a_question_that_was_left_as_one_has_nothing_wrong(self) -> None:
        assert judge(QUESTION, done(None), None) == []

    def test_a_question_that_was_taken_for_a_command_is_said(self) -> None:
        taken = done("volume-status")

        assert judge(QUESTION, taken, {}) == [
            "done instead of no_skill",
            "skill volume-status instead of None",
        ]

    def test_another_status_is_said(self) -> None:
        asked = done("volume", ActStatus.NEEDS_MORE)

        assert judge(VOLUME, asked, None) == ["needs_more instead of done", "nothing was run"]

    def test_another_skill_is_said_and_its_values_are_not_held_against_it(self) -> None:
        wrong = done("volume-change")

        assert judge(VOLUME, wrong, {"direction": "up"}) == [
            "skill volume-change instead of volume",
            "level=None instead of 30",
        ]

    def test_a_value_that_is_not_the_one_said_is_said(self) -> None:
        assert judge(VOLUME, done("volume"), {"level": 40}) == ["level=40 instead of 30"]

    def test_a_value_that_is_missing_is_said(self) -> None:
        assert judge(VOLUME, done("volume"), {}) == ["level=None instead of 30"]

    def test_a_value_that_nobody_said_is_said_when_the_skill_was_the_right_one(self) -> None:
        assert judge(VOLUME, done("volume"), {"level": 30, "unit": "percent"}) == [
            "unit='percent' was run and nobody said it"
        ]

    def test_a_value_that_code_reads_the_same_is_not_a_mistake_of_the_model(self) -> None:
        change = PlanCase(
            "sposta la sveglia delle sette alle otto",
            KITCHEN,
            "alarm-change",
            ActStatus.DONE,
            None,
            {"time": "otto", "from": "sette"},
        )

        assert (
            judge(change, done("alarm-change"), {"time": "alle otto", "from": "delle sette"}) == []
        )
        assert judge(
            change, done("alarm-change"), {"time": "alle nove", "from": "delle sette"}
        ) == ["time='alle nove' instead of 'otto'"]

    def test_a_value_is_the_same_when_only_its_case_differs(self) -> None:
        assert (
            judge(LAMP_ON, done("lamp"), {"state": "ON"}, {"entity_id": "light.living_room"}) == []
        )

    def test_a_skill_that_was_not_called_is_said_when_it_had_to_be(self) -> None:
        assert judge(VOLUME, done("volume"), None) == ["nothing was run"]

    def test_a_skill_that_was_not_called_is_not_wrong_when_it_was_not_to_be(self) -> None:
        asked_for_more = PlanCase("set the volume", KITCHEN, "volume", ActStatus.NEEDS_MORE)

        assert judge(asked_for_more, done("volume", ActStatus.NEEDS_MORE), None) == []

    def test_a_skill_of_the_platform_is_judged_by_the_values_it_was_called_with(self) -> None:
        """It does not go through the runner: the values are those of the trail."""
        assert judge(VOLUME, done("volume"), {"level": 30}, None) == []

    def test_the_device_a_script_was_bound_to_is_the_one_that_was_meant(self) -> None:
        bedroom = {"entity_id": "light.bedroom"}

        assert judge(LAMP_ON, done("lamp"), {"state": "on"}, bedroom) == [
            "device light.bedroom instead of Living room lamp"
        ]
        assert judge(LAMP_ON, done("lamp"), {"state": "on"}, {}) == [
            "device None instead of Living room lamp"
        ]

    def test_every_external_device_of_the_example_home_has_its_entity(self) -> None:
        assert set(ENTITIES) == {"Living room lamp", "Bedroom lamp", "Garage door"}

    def test_an_announcement_is_judged_by_the_words_that_were_said(self) -> None:
        said = ["Dinner is ready."]

        assert judge(ANNOUNCE, done("announce"), {"message": "dinner is ready"}, None, said) == []
        assert judge(ANNOUNCE, done("announce"), None, None, []) == ["nothing was said"]
        assert judge(ANNOUNCE, done("announce"), None, None, ["the dinner is ready"]) == [
            "'the dinner is ready' was said instead of 'dinner is ready'"
        ]

    def test_an_announcement_that_was_asked_about_first_has_nothing_to_say_yet(self) -> None:
        asked = PlanCase(
            "announce that dinner is ready",
            KITCHEN,
            "announce",
            ActStatus.NEEDS_CONFIRMATION,
            None,
            {"message": "dinner is ready"},
        )

        assert judge(asked, done("announce", ActStatus.NEEDS_CONFIRMATION), None) == []


class TestJudged:
    def test_it_is_right_when_nothing_is_wrong(self) -> None:
        assert Judged(QUESTION, done(None), 0.1, None, []).right
        assert not Judged(QUESTION, done(None), 0.1, None, ["something"]).right


def ideal_plan(case: PlanCase) -> dict[str, Any]:
    if case.skill is None:
        return plan_of(None, reason="a question, not a request")
    asks = case.status is ActStatus.NEEDS_CONFIRMATION
    return plan_of(
        case.skill,
        device=case.device,
        parameters=case.params,
        question="Do you really want me to do that?" if asks else None,
    )


def a_rig() -> ActionsRig:
    rig = ActionsRig(
        home_text=HOME_TEXT, core=platform_skills(FakeSpeech()), every_skill_is_offered=True
    )
    rig.dispatcher.responder = a_device_answers
    return rig


class TestMeasuring:
    """The harness against a planner that says the right thing, for what the code does with it."""

    async def test_a_skill_of_the_platform_is_measured_by_what_it_was_called_with(self) -> None:
        rig = a_rig()
        rig.plans(ideal_plan(VOLUME))

        judged = await measure(rig, VOLUME)

        assert judged.problems == []
        assert judged.values == {"level": 30}
        assert rig.runner.requests == []  # no runner was involved
        assert judged.seconds >= 0

    async def test_what_a_skill_was_called_with_is_what_the_code_made_of_the_plan(self) -> None:
        """A level that nobody said is left out before the skill is called (ADR 0028), and the
        harness sees what was run and not what the model wrote."""
        rig = a_rig()
        invented = plan_of("volume-change", parameters={"direction": "up", "amount": 50})
        rig.plans(invented)
        case = PlanCase(
            "turn the volume up",
            KITCHEN,
            "volume-change",
            ActStatus.DONE,
            None,
            {"direction": "up"},
        )

        judged = await measure(rig, case)

        assert judged.values == {"direction": "up"}
        assert judged.problems == []

    async def test_a_model_that_reads_it_wrong_is_judged_wrong(self) -> None:
        rig = a_rig()
        rig.plans(plan_of("mute", parameters={"muted": True}))

        judged = await measure(rig, VOLUME)

        assert not judged.right
        assert "skill mute instead of volume" in judged.problems

    async def test_a_message_that_is_not_for_a_skill_has_no_values(self) -> None:
        rig = a_rig()
        rig.plans(ideal_plan(QUESTION))

        judged = await measure(rig, QUESTION)

        assert judged.values is None
        assert judged.problems == []

    async def test_the_message_before_it_does_not_count_for_the_one_after(self) -> None:
        """The newest record of the trail is this message's; a message that left none has none."""
        rig = a_rig()
        rig.plans(ideal_plan(VOLUME), ideal_plan(QUESTION))

        first = await measure(rig, VOLUME)
        second = await measure(rig, QUESTION)

        assert first.values == {"level": 30}
        assert second.values is None

    async def test_the_script_that_ran_for_the_message_before_is_not_this_ones(self) -> None:
        lamp = PlanCase(
            "turn on the living room lamp",
            KITCHEN,
            "lamp",
            ActStatus.DONE,
            "Living room lamp",
            {"state": "on"},
        )
        garage = PlanCase(
            "open the garage door",
            DISPLAY,
            "garage-door",
            ActStatus.NEEDS_CONFIRMATION,
            "Garage door",
            {"action": "open"},
        )
        rig = a_rig()
        await load_examples(rig)
        rig.plans(ideal_plan(lamp), ideal_plan(VOLUME), ideal_plan(garage))

        first = await measure(rig, lamp)
        second = await measure(rig, VOLUME)
        third = await measure(rig, garage)

        assert first.problems == []
        assert len(rig.runner.requests) == 1  # the lamp: the others ran no script
        assert second.values == {"level": 30}
        assert second.problems == []
        # asked about first, so nothing ran: the lamp of the first is not the device of this one
        assert third.problems == []

    async def test_the_words_said_for_the_message_before_are_not_said_again(self) -> None:
        dinner = PlanCase(
            "tell everyone that dinner is ready",
            KITCHEN,
            "announce",
            ActStatus.DONE,
            None,
            {"message": "dinner is ready"},
        )
        guests = PlanCase(
            "announce that the guests have arrived",
            DISPLAY,
            "announce",
            ActStatus.DONE,
            None,
            {"message": "the guests have arrived"},
        )
        rig = a_rig()
        rig.plans(ideal_plan(dinner), ideal_plan(guests))

        first = await measure(rig, dinner)
        second = await measure(rig, guests)

        assert first.problems == []
        assert second.problems == []

    async def test_a_yes_that_was_asked_for_is_not_carried_to_the_next_message(self) -> None:
        asked = PlanCase(
            "open the garage door",
            DISPLAY,
            "garage-door",
            ActStatus.NEEDS_CONFIRMATION,
            "Garage door",
            {"action": "open"},
        )
        rig = a_rig()
        skills = await load_examples(rig)
        assert "garage-door" in skills
        rig.plans(ideal_plan(asked), ideal_plan(asked))

        once = await measure(rig, asked)
        again = await measure(rig, asked)

        assert once.problems == again.problems == []
        assert once.values == {"action": "open"}


async def load_examples(rig: ActionsRig) -> set[str]:
    """The skills of the example folder, installed and enabled."""
    loaded, problems = load_folder(EXAMPLES)
    assert problems == []
    return set(await rig.install(*(skill.manifest for skill in loaded), index=False))
