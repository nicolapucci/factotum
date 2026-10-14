import asyncio
import json
from typing import Any

import pytest
from pydantic import ValidationError

from factotum.actions.planner import (
    EXAMPLES,
    MAX_CHARS,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    Plan,
    PlanDevices,
    Planner,
    SkillView,
    render,
    shortened,
    view_of,
)
from factotum.actions.testing import GARAGE, LAMP, VOLUME, WEATHER, ScriptedChat
from factotum.contracts.devices import ExternalDeviceRecord
from factotum.contracts.errors import GatewayError, GatewayUnavailableError, ProviderTimeoutError
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.models import ModelRole
from factotum.contracts.skills import ParameterSpec, ParamType, SkillTarget
from factotum.contracts.testing import make_external_device, make_tenant_id

TENANT = make_tenant_id()
OWNER = Owner.of_subject(Subject.tenant(TENANT))


def lamps() -> list[ExternalDeviceRecord]:
    return [
        make_external_device(tenant_id=TENANT, name="Living room lamp").model_copy(
            update={"aliases": ("lamp", "the lamp")}
        ),
        make_external_device(tenant_id=TENANT, name="Bedroom lamp"),
    ]


class TestThePlan:
    def test_it_says_why_first_and_then_what(self) -> None:
        assert next(iter(Plan.model_fields)) == "reason"
        assert next(iter(RESPONSE_SCHEMA["properties"])) == "reason"

    def test_a_plan_that_asks_for_nothing_is_a_plan(self) -> None:
        plan = Plan.model_validate({"reason": "asks a question", "skill": None})

        assert (plan.skill, plan.device, plan.parameters, plan.question) == (None, None, {}, None)
        assert plan.language == "en"

    @pytest.mark.parametrize(
        ("written", "meant"),
        [
            ("it", "it"),
            ("IT", "it"),
            (" it ", "it"),
            ("it-IT", "it"),
            ("it_IT", "it"),
            ("Italian", "it"),
            ("italiano", "it"),
            ("English", "en"),
            ("eng", "eng"),
            ("", "en"),
            (None, "en"),
            ("klingon", "en"),
            ("i", "en"),
            (12, "en"),
        ],
    )
    def test_whatever_the_model_wrote_for_the_language_becomes_a_code(
        self, written: Any, meant: str
    ) -> None:
        assert Plan.model_validate({"reason": "x", "language": written}).language == meant

    def test_a_plan_can_be_long_winded_without_being_refused(self) -> None:
        plan = Plan.model_validate(
            {"reason": "x" * 400, "question": "y" * 900, "ask": "z" * 900, "skill": "lamp"}
        )

        assert plan.skill == "lamp"

    def test_a_runaway_answer_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Plan.model_validate({"reason": "x" * 501})
        with pytest.raises(ValidationError):
            Plan.model_validate({"reason": "x", "question": "y" * 1001})

    def test_what_the_model_was_not_asked_for_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Plan.model_validate({"reason": "x", "speaker": "ada"})


class TestTheExamples:
    def test_every_example_is_a_plan(self) -> None:
        for example in EXAMPLES:
            Plan.model_validate(example.plan)

    def test_what_an_example_chooses_is_among_the_skills_it_shows(self) -> None:
        for example in EXAMPLES:
            chosen = example.plan["skill"]
            assert chosen is None or chosen in {skill.name for skill in example.skills}, example

    def test_a_device_an_example_names_is_one_of_those_of_the_skill_it_chooses(self) -> None:
        for example in EXAMPLES:
            device = example.plan["device"]
            if device is None:
                continue
            [skill] = [s for s in example.skills if s.name == example.plan["skill"]]
            assert any(entry.startswith(device) for entry in skill.devices), example

    def test_the_messages_are_all_different(self) -> None:
        messages = [example.message for example in EXAMPLES]

        assert len(set(messages)) == len(messages)

    def test_there_are_messages_that_ask_and_messages_that_do_not_and_both_languages(self) -> None:
        skills = [example.plan["skill"] for example in EXAMPLES]
        languages = {example.plan["language"] for example in EXAMPLES}

        assert None in skills
        assert any(skill is not None for skill in skills)
        assert {"en", "it"} <= languages

    def test_a_question_about_how_to_do_it_is_not_an_action_in_the_examples(self) -> None:
        how = [e for e in EXAMPLES if e.message.lower().startswith("how do i")]

        assert how
        assert all(e.plan["skill"] is None for e in how)

    def test_an_example_is_shown_as_the_model_will_be_asked_and_answers_in_json(self) -> None:
        for example in EXAMPLES:
            assert example.user().endswith(f"Message: {example.message}")
            assert json.loads(example.answer()) == example.plan

    def test_an_example_that_names_a_missing_value_asks_for_it(self) -> None:
        [weather] = [e for e in EXAMPLES if e.message == "What's the weather like?"]

        assert weather.plan["skill"] == "weather"
        assert weather.plan["ask"]


class TestWhatTheExamplesTeach:
    """What a small model got wrong when it was measured with the skills of the platform (llama3.1
    8B, ADR 0029), and the example that shows it the other way. The model is not asked here: what is
    held is that the example is there, and that it says it."""

    @staticmethod
    def example(message: str) -> Any:
        [found] = [e for e in EXAMPLES if e.message == message]
        return found

    def test_a_noise_that_is_not_named_is_stopped_and_not_muted(self) -> None:
        for message in ("Basta, grazie", "Quiet please"):
            assert self.example(message).plan["skill"] == "stop"
        assert {"mute", "stop"} <= {s.name for s in self.example("Quiet please").skills}

    def test_a_timer_that_is_named_is_cancelled_and_not_stopped(self) -> None:
        example = self.example("Stop my timer please")

        assert example.plan["skill"] == "timer-cancel"
        assert "stop" in {s.name for s in example.skills}

    def test_a_reminder_is_a_timer_in_the_words_that_were_said(self) -> None:
        example = self.example("Ricordamelo tra un quarto d'ora")

        assert example.plan["skill"] == "timer"
        assert example.plan["parameters"] == {"duration": "un quarto d'ora"}
        assert example.plan["language"] == "it"
        assert "alarm-postpone" in {s.name for s in example.skills}

    def test_putting_an_alarm_off_with_no_length_leaves_the_length_out(self) -> None:
        example = self.example("Posticipa la sveglia")

        assert example.plan["skill"] == "alarm-postpone"
        assert example.plan["parameters"] == {}

    def test_an_hour_is_copied_as_it_was_said_and_not_written_another_way(self) -> None:
        example = self.example("Svegliami alle sei e un quarto")

        assert example.plan["skill"] == "alarm"
        assert example.plan["parameters"] == {"time": "sei e un quarto"}

    def test_there_is_no_example_that_sets_the_volume_among_these(self) -> None:
        """An example of a level (*Porta il volume a 35*) made the model read *alza il volume a 70*
        and *bump the volume up to 80* as a turn of the volume (measured, three of three): the
        volume is left to the examples it had, which read it well."""
        assert not [
            e for e in EXAMPLES if e.plan["skill"] == "volume" and e.plan["language"] == "it"
        ]

    def test_the_words_of_an_english_message_stay_english(self) -> None:
        """Three examples in Italian that copy the words made the model write *otto* and *domani*
        for *eight* and *monday*: the same is shown in English."""
        example = self.example("Wake me up on Friday at half past six")

        assert example.plan["skill"] == "alarm"
        assert example.plan["parameters"] == {"time": "half past six", "day": "Friday"}
        assert example.plan["language"] == "en"

    def test_the_clock_is_asked_the_time_and_is_not_asked_the_date_of_another_day(self) -> None:
        assert self.example("Do you know what time it is?").plan["parameters"] == {"about": "time"}
        elsewhere = self.example("What will the date be next Friday?")

        assert elsewhere.plan["skill"] is None
        assert "clock" in {s.name for s in elsewhere.skills}

    def test_the_words_of_a_value_that_a_skill_reads_are_copied_and_not_translated(self) -> None:
        assert "copied in the very words and language of the message" in SYSTEM_PROMPT
        assert "never translated" in SYSTEM_PROMPT

    def test_the_examples_name_the_skill_by_the_name_it_has(self) -> None:
        names = {skill.name for example in EXAMPLES for skill in example.skills}
        chosen = {e.plan["skill"] for e in EXAMPLES if e.plan["skill"] is not None}

        assert chosen <= names


class TestWhatTheModelIsShown:
    def test_a_skill_is_shown_with_what_it_is_for_the_values_it_takes_and_the_devices(self) -> None:
        view = view_of(LAMP, list(lamps()))

        assert view == SkillView(
            name="lamp",
            description="Turn a lamp on or off, or set how bright it is.",
            target=SkillTarget.EXTERNAL,
            values=("state (on or off, required)", "brightness (whole number 0 to 100)"),
            devices=("Living room lamp (also called: lamp, the lamp)", "Bedroom lamp"),
        )

    def test_the_kinds_of_value_are_said_in_words(self) -> None:
        assert view_of(WEATHER).values == ("place (text, required)",)
        assert view_of(VOLUME).values == ("level (whole number 0 to 100, required)",)
        assert view_of(GARAGE).values == ("action (open or close, required)",)

    def test_what_a_value_is_for_is_said_after_its_kind(self) -> None:
        """A description is how a skill tells the small model that it is to copy the words that were
        said, or that a brightness alone means on. It was written in every skill and shown to
        nobody."""
        described = LAMP.model_copy(
            update={
                "parameters": {
                    "state": ParameterSpec(
                        type=ParamType.STRING,
                        enum=("on", "off"),
                        description="on, when only a brightness is asked for",
                    ),
                    "brightness": ParameterSpec(type=ParamType.INTEGER, minimum=0, maximum=100),
                }
            }
        )

        assert view_of(described).values == (
            "state (on or off, required): on, when only a brightness is asked for",
            "brightness (whole number 0 to 100)",
        )

    def test_the_skills_and_the_message_are_one_turn(self) -> None:
        text = render(
            [view_of(LAMP, list(lamps())), view_of(VOLUME)],
            PlanDevices(here="Kitchen speaker", others=("Study player",)),
            "turn on the lamp",
        )

        assert text.splitlines() == [
            "Skills:",
            "- lamp: Turn a lamp on or off, or set how bright it is.",
            "  values: state (on or off, required); brightness (whole number 0 to 100)",
            "  devices: Living room lamp (also called: lamp, the lamp); Bedroom lamp",
            "- volume: Set the volume of the device that is being spoken to.",
            "  values: level (whole number 0 to 100, required)",
            "  acts on: the device that is spoken to, unless another is named",
            "The device that is spoken to: Kitchen speaker",
            "Other devices: Study player",
            "Message: turn on the lamp",
        ]

    def test_a_skill_that_acts_on_nothing_has_no_devices_line(self) -> None:
        text = render([view_of(WEATHER)], PlanDevices(), "weather in Rome")

        assert text.splitlines() == [
            "Skills:",
            "- weather: Say what the weather is like now in a city.",
            "  values: place (text, required)",
            "Message: weather in Rome",
        ]

    def test_a_message_that_is_too_long_is_read_by_its_two_ends(self) -> None:
        message = "open the garage " + "blah " * 500 + "right now"

        shown = shortened(message)

        assert shown.startswith("open the garage")
        assert shown.endswith("right now")
        assert " … " in shown
        assert len(shown) <= MAX_CHARS + 5
        assert shortened("short") == "short"


class TestAskingTheModel:
    def request_of(self, chat: ScriptedChat) -> Any:
        [request] = chat.asked
        return request

    async def test_the_model_is_asked_in_the_fast_role_with_a_schema_and_no_temperature(
        self,
    ) -> None:
        chat = ScriptedChat()
        chat.say({"reason": "asks", "skill": None})

        await Planner(chat).plan(
            OWNER, "hello", [view_of(WEATHER)], PlanDevices(here="Kitchen speaker")
        )

        request = self.request_of(chat)
        assert request.role is ModelRole.CHAT_FAST
        assert request.temperature == 0
        assert request.response_schema == RESPONSE_SCHEMA
        assert request.max_output_tokens == 300

    async def test_it_is_told_what_to_do_shown_examples_as_turns_and_then_asked(self) -> None:
        chat = ScriptedChat()
        chat.say({"reason": "asks", "skill": None})

        await Planner(chat).plan(OWNER, "hello", [view_of(WEATHER)], PlanDevices())

        request = self.request_of(chat)
        roles = [m.role.value for m in request.messages]
        assert request.messages[0].content == SYSTEM_PROMPT
        assert roles == ["system", *["user", "assistant"] * len(EXAMPLES), "user"]
        assert request.messages[-1].content.endswith("Message: hello")
        assert request.messages[-1].content.startswith("Skills:\n- weather:")

    async def test_it_is_asked_for_the_owner_of_the_person_who_spoke(self) -> None:
        chat = ScriptedChat()
        chat.say({"reason": "asks", "skill": None})

        await Planner(chat).plan(OWNER, "hello", [], PlanDevices())

        assert chat.requests[0][0] == OWNER

    async def test_what_the_model_says_is_a_plan(self) -> None:
        chat = ScriptedChat()
        chat.say(
            {
                "reason": "asks to turn on a lamp",
                "skill": "lamp",
                "device": "Living room lamp",
                "parameters": {"state": "on"},
                "language": "Italian",
                "question": "Vuoi che accenda la lampada?",
            }
        )

        plan = await Planner(chat).plan(OWNER, "accendi la lampada", [view_of(LAMP)], PlanDevices())

        assert (plan.skill, plan.device, plan.parameters, plan.language) == (
            "lamp",
            "Living room lamp",
            {"state": "on"},
            "it",
        )

    async def test_a_model_that_does_not_answer_in_time_is_a_timeout(self) -> None:
        class Slow(ScriptedChat):
            async def chat(self, owner: Owner, request: Any) -> Any:
                await asyncio.sleep(5)

        with pytest.raises(ProviderTimeoutError):
            await Planner(Slow(), timeout_s=0.05).plan(OWNER, "hello", [], PlanDevices())

    @pytest.mark.parametrize("nonsense", [{"skill": "lamp"}, {"reason": "x", "extra": 1}, "lamp"])
    async def test_an_answer_that_is_not_a_plan_is_an_error_and_not_a_guess(
        self, nonsense: Any
    ) -> None:
        chat = ScriptedChat()
        chat.say(nonsense)

        with pytest.raises(GatewayError, match="not a plan"):
            await Planner(chat).plan(OWNER, "hello", [], PlanDevices())

    async def test_a_gateway_that_is_away_is_the_error_it_was(self) -> None:
        chat = ScriptedChat()
        chat.say(GatewayUnavailableError("the model gateway could not be reached"))

        with pytest.raises(GatewayUnavailableError):
            await Planner(chat).plan(OWNER, "hello", [], PlanDevices())
