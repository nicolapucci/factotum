import math
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.formats import convert
from factotum.actions.grounding import says
from factotum.actions.values import MAX_TEXT_CHARS, check_specs, check_values
from factotum.contracts.effects import Effect
from factotum.contracts.skills import ParameterSpec, ParamType, SkillManifest

MANIFEST = SkillManifest(
    name="lamp",
    description="Turns a lamp on or off, or sets how bright it is.",
    effect=Effect.REVERSIBLE,
    parameters={
        "state": ParameterSpec(type=ParamType.STRING, enum=("on", "off")),
        "brightness": ParameterSpec(type=ParamType.INTEGER, minimum=0, maximum=100),
        "warmth": ParameterSpec(type=ParamType.NUMBER, minimum=0, maximum=1),
        "smooth": ParameterSpec(type=ParamType.BOOLEAN),
        "label": ParameterSpec(type=ParamType.STRING),
        "steps": ParameterSpec(type=ParamType.INTEGER, enum=(1, 5, 10)),
    },
    required=("state",),
)


NEEDS_A_LEVEL = SkillManifest(
    name="volume",
    description="Sets the volume.",
    effect=Effect.REVERSIBLE,
    parameters={"level": ParameterSpec(type=ParamType.INTEGER, minimum=0, maximum=100)},
    required=("level",),
)


def check(**given: Any) -> Any:
    """What is accepted of a value, whatever was said: the type, the bounds and the choices."""
    return check_specs(MANIFEST.parameters, MANIFEST.required, given)


class TestWhatIsAccepted:
    def test_values_of_the_declared_types_are_accepted_as_they_are(self) -> None:
        checked = check(
            state="on", brightness=40, warmth=0.5, smooth=True, label="the lamp", steps=5
        )

        assert checked.ok
        assert checked.values == {
            "state": "on",
            "brightness": 40,
            "warmth": 0.5,
            "smooth": True,
            "label": "the lamp",
            "steps": 5,
        }

    def test_what_is_not_required_may_be_left_out(self) -> None:
        checked = check(state="off")

        assert checked.ok
        assert checked.values == {"state": "off"}

    def test_a_value_that_is_null_is_a_value_that_was_not_given(self) -> None:
        checked = check(state="on", brightness=None, label=None)

        assert checked.ok
        assert checked.values == {"state": "on"}

    def test_names_the_skill_did_not_declare_are_dropped(self) -> None:
        checked = check(state="on", device="Living room lamp", volume=11)

        assert checked.ok
        assert checked.values == {"state": "on"}


class TestSlipsThatLeaveNoDoubt:
    @pytest.mark.parametrize("written", ["On", " ON ", "on", "oN"])
    def test_a_choice_in_another_case_is_the_choice(self, written: str) -> None:
        assert check(state=written).values == {"state": "on"}

    @pytest.mark.parametrize(
        ("written", "meant"), [("30", 30), (" 30 ", 30), ("30%", 30), (30.0, 30), ("0", 0)]
    )
    def test_a_whole_number_written_as_text_or_with_a_point_is_the_number(
        self, written: Any, meant: int
    ) -> None:
        assert check(state="on", brightness=written).values["brightness"] == meant

    def test_a_number_written_as_text_is_the_number(self) -> None:
        assert check(state="on", warmth="0.25").values["warmth"] == 0.25

    @pytest.mark.parametrize(
        ("written", "meant"),
        [("true", True), ("Yes", True), ("on", True), ("1", True), ("no", False), ("OFF", False)],
    )
    def test_a_yes_or_a_no_in_words_is_a_boolean(self, written: str, meant: bool) -> None:
        assert check(state="on", smooth=written).values["smooth"] is meant

    def test_a_number_for_a_text_is_its_digits_and_spaces_are_squeezed(self) -> None:
        assert check(state="on", label=12).values["label"] == "12"
        assert check(state="on", label="  the   big \t lamp ").values["label"] == "the big lamp"


class TestWhatIsNotAccepted:
    @pytest.mark.parametrize(
        ("name", "written"),
        [
            ("state", "dim"),  # not a choice
            ("state", 1),
            ("state", True),
            ("brightness", "very bright"),
            ("brightness", 101),  # over the maximum
            ("brightness", -1),
            ("brightness", 40.5),  # not whole
            ("brightness", True),
            ("brightness", float("nan")),
            ("brightness", float("inf")),
            ("brightness", [40]),
            ("warmth", 1.5),
            ("warmth", "warm"),
            ("smooth", "maybe"),
            ("smooth", 1),
            ("label", ""),
            ("label", "   "),
            ("label", "x" * (MAX_TEXT_CHARS + 1)),
            ("label", "two\nlines\x00"),
            ("label", {"a": 1}),
            ("steps", 3),  # not a choice
        ],
    )
    def test_it_is_invalid_and_the_person_is_asked_again(self, name: str, written: Any) -> None:
        checked = check(**{"state": "on", name: written})

        assert not checked.ok
        assert checked.invalid == (name,)
        assert name not in checked.values
        assert checked.asked_for == (name,)

    def test_a_required_value_that_is_not_given_is_missing(self) -> None:
        checked = check(brightness=10)

        assert checked.missing == ("state",)
        assert checked.values == {"brightness": 10}
        assert checked.asked_for == ("state",)

    def test_a_required_value_that_is_wrong_is_invalid_and_not_also_missing(self) -> None:
        checked = check(state="dim")

        assert checked.missing == ()
        assert checked.invalid == ("state",)

    def test_what_is_missing_and_what_is_invalid_are_both_asked_for(self) -> None:
        checked = check(brightness=500)

        assert checked.asked_for == ("state", "brightness")


@given(
    st.dictionaries(
        st.sampled_from([*MANIFEST.parameters, "extra"]),
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.floats(allow_nan=True, allow_infinity=True),
            st.text(max_size=20),
            st.lists(st.integers(), max_size=2),
        ),
    )
)
def test_whatever_a_model_says_what_is_accepted_is_what_the_skill_declares(
    given_values: dict[str, Any],
) -> None:
    checked = check_specs(MANIFEST.parameters, MANIFEST.required, given_values)

    for name, value in checked.values.items():
        spec = MANIFEST.parameters[name]
        match spec.type:
            case ParamType.STRING:
                assert isinstance(value, str)
                assert 0 < len(value) <= MAX_TEXT_CHARS
            case ParamType.BOOLEAN:
                assert isinstance(value, bool)
            case ParamType.INTEGER:
                assert isinstance(value, int)
                assert not isinstance(value, bool)
            case ParamType.NUMBER:
                assert isinstance(value, int | float)
                assert not isinstance(value, bool)
                assert math.isfinite(value)
        if spec.enum is not None:
            assert value in spec.enum
        if spec.minimum is not None and not isinstance(value, str | bool):
            assert value >= spec.minimum
        if spec.maximum is not None and not isinstance(value, str | bool):
            assert value <= spec.maximum
    assert set(checked.values) <= set(MANIFEST.parameters)
    assert not set(checked.values) & set(checked.invalid)
    assert not set(checked.missing) & set(checked.values)
    assert set(MANIFEST.required) <= set(checked.values) | set(checked.missing) | set(
        checked.invalid
    )


class TestWhatThePersonSaid:
    """A value of the right type that nobody said is not a value (ADR 0028)."""

    def test_a_number_that_the_message_gives_is_a_value(self) -> None:
        checked = check_values(
            MANIFEST, {"state": "on", "brightness": 40}, said="the lamp to 40 percent"
        )

        assert checked.ok
        assert checked.values == {"state": "on", "brightness": 40}
        assert checked.unsaid == ()

    def test_a_number_that_it_does_not_give_is_left_out_and_the_rest_is_kept(self) -> None:
        checked = check_values(
            MANIFEST, {"state": "on", "brightness": 100}, said="turn the lamp on"
        )

        assert checked.ok
        assert checked.values == {"state": "on"}
        assert checked.unsaid == ("brightness",)
        assert checked.asked_for == ()

    def test_a_required_value_that_it_does_not_give_is_missing_and_the_person_is_asked(
        self,
    ) -> None:
        checked = check_values(NEEDS_A_LEVEL, {"level": 100}, said="turn it up")

        assert not checked.ok
        assert checked.values == {}
        assert checked.missing == ("level",)
        assert checked.unsaid == ("level",)
        assert checked.asked_for == ("level",)

    def test_a_text_is_what_the_message_has_words_for(self) -> None:
        said = check_values(
            MANIFEST, {"state": "on", "label": "the lamp"}, said="switch the lamp on"
        )
        thought_of = check_values(
            MANIFEST, {"state": "on", "label": "the vase"}, said="switch the lamp on"
        )

        assert said.values["label"] == "the lamp"
        assert thought_of.unsaid == ("label",)
        assert "label" not in thought_of.values

    def test_a_choice_and_a_yes_or_no_are_not_looked_for_in_the_words(self) -> None:
        checked = check_values(
            MANIFEST, {"state": "on", "smooth": True, "steps": 5}, said="accendi la luce"
        )

        assert checked.values == {"state": "on", "smooth": True, "steps": 5}
        assert checked.unsaid == ()

    def test_a_value_that_is_wrong_is_invalid_and_not_unsaid(self) -> None:
        checked = check_values(MANIFEST, {"state": "on", "brightness": 500}, said="at 500")

        assert checked.invalid == ("brightness",)
        assert checked.unsaid == ()

    def test_an_empty_message_says_nothing_and_is_not_a_reason_to_believe_everything(self) -> None:
        checked = check_values(MANIFEST, {"state": "on", "brightness": 0}, said="")

        assert checked.values == {"state": "on"}
        assert checked.unsaid == ("brightness",)

    def test_the_values_of_a_device_are_not_looked_for_in_any_words(self) -> None:
        """No person says the entity of a lamp: they come from the file of the home."""
        checked = check_specs(
            MANIFEST.parameters, MANIFEST.required, {"state": "on", "brightness": 100}
        )

        assert checked.values == {"state": "on", "brightness": 100}
        assert checked.unsaid == ()


@given(
    st.dictionaries(
        st.sampled_from([*MANIFEST.parameters, "extra"]),
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(-5, 120),
            st.floats(allow_nan=True, allow_infinity=True),
            st.text(max_size=12),
        ),
    ),
    st.text(max_size=40),
)
def test_whatever_a_model_writes_what_is_kept_is_what_the_message_says(
    given_values: dict[str, Any], message: str
) -> None:
    checked = check_values(MANIFEST, given_values, said=message)

    for name, value in checked.values.items():
        assert says(message, MANIFEST.parameters[name], value)
    assert not set(checked.values) & set(checked.unsaid)
    assert not set(checked.invalid) & set(checked.unsaid)
    for name in checked.unsaid:
        if name in MANIFEST.required:
            assert name in checked.missing


MEETING = SkillManifest(
    name="meeting-add",
    description="Adds a meeting to the calendar.",
    effect=Effect.REVERSIBLE,
    parameters={
        "title": ParameterSpec(type=ParamType.STRING),
        "day": ParameterSpec.model_validate(
            {"type": "string", "formats": [{"date": "dd/MM/yy"}, {"in_days": "+{n}d"}]}
        ),
        "at": ParameterSpec.model_validate({"type": "string", "formats": [{"time": "HH:mm"}]}),
        "length": ParameterSpec.model_validate(
            {"type": "integer", "minimum": 5, "maximum": 480, "formats": [{"duration": "minutes"}]}
        ),
    },
    required=("title", "day"),
)


class TestAValueWithAFormat:
    """What a skill is given for a value that is said in words is what code reads in the message,
    written in the form the skill declared (ADR 0030)."""

    SAID = "metti il pranzo domani alle sette e mezza per un'ora"

    def test_what_was_said_is_given_in_the_forms_the_skill_declared(self) -> None:
        checked = check_values(
            MEETING,
            {"title": "il pranzo", "day": "domani", "at": "sette e mezza", "length": "un'ora"},
            said=self.SAID,
        )

        assert checked.ok
        assert checked.values == {
            "title": "il pranzo",
            "day": "+1d",
            "at": "07:30",
            "length": 60,
        }
        assert checked.unsaid == ()

    def test_a_model_may_write_the_form_of_what_was_said_and_not_its_words(self) -> None:
        checked = check_values(
            MEETING,
            {"title": "pranzo", "day": "tomorrow", "at": "7:30", "length": "60 minutes"},
            said=self.SAID,
        )

        assert checked.values == {"title": "pranzo", "day": "+1d", "at": "07:30", "length": 60}

    def test_an_optional_value_that_was_not_said_is_left_out_and_the_rest_is_kept(self) -> None:
        checked = check_values(
            MEETING, {"title": "pranzo", "day": "domani", "at": "9:00"}, said=self.SAID
        )

        assert checked.ok
        assert checked.values == {"title": "pranzo", "day": "+1d"}
        assert checked.unsaid == ("at",)
        assert checked.asked_for == ()

    def test_a_required_value_that_was_not_said_is_missing_and_the_person_is_asked(self) -> None:
        checked = check_values(
            MEETING, {"title": "pranzo", "day": "dopodomani"}, said="metti il pranzo domani"
        )

        assert not checked.ok
        assert checked.missing == ("day",)
        assert checked.unsaid == ("day",)
        assert checked.asked_for == ("day",)

    def test_a_day_that_no_form_of_the_skill_can_say_is_not_a_value(self) -> None:
        """Monday is a day of the week, and the skill takes a date or a number of days."""
        checked = check_values(
            MEETING, {"title": "pranzo", "day": "lunedì"}, said="metti il pranzo lunedì"
        )

        assert checked.missing == ("day",)
        assert checked.unsaid == ("day",)

    def test_what_cannot_be_read_is_not_a_value_and_is_not_invalid_either(self) -> None:
        """A model that wrote something that is no hour at all: left out, not asked about again."""
        checked = check_values(
            MEETING,
            {"title": "pranzo", "day": "domani", "at": "now", "length": "a while"},
            said=self.SAID,
        )

        assert checked.ok
        assert checked.unsaid == ("at", "length")
        assert checked.invalid == ()

    @pytest.mark.parametrize(
        ("written", "message"),
        [("dieci ore", "registra per dieci ore"), ("un minuto", "registra per un minuto")],
    )
    def test_a_value_that_was_said_and_is_out_of_the_bounds_is_invalid(
        self, written: str, message: str
    ) -> None:
        checked = check_values(
            MEETING,
            {"title": "pranzo", "day": "domani", "length": written},
            said=f"pranzo domani {message}",
        )

        assert checked.invalid == ("length",)
        assert checked.unsaid == ()
        assert not checked.ok

    def test_the_other_values_of_the_skill_are_checked_as_they_were(self) -> None:
        checked = check_values(
            MEETING, {"title": "il vaso", "day": "domani"}, said="metti il pranzo domani"
        )

        assert checked.unsaid == ("title",)
        assert checked.missing == ("title",)

    def test_a_value_that_is_set_up_and_not_said_is_not_converted(self) -> None:
        checked = check_specs(
            MEETING.parameters, (), {"at": "07:30", "day": "+1d", "title": "x", "length": 30}
        )

        assert checked.values == {"at": "07:30", "day": "+1d", "title": "x", "length": 30}

    def test_a_formatted_value_that_is_not_given_is_missing_when_it_is_required(self) -> None:
        checked = check_values(MEETING, {"title": "pranzo"}, said="metti il pranzo domani")

        assert checked.missing == ("day",)
        assert checked.unsaid == ()

    @given(
        st.dictionaries(
            st.sampled_from(["title", "day", "at", "length", "extra"]),
            st.one_of(st.none(), st.integers(-5, 600), st.text(max_size=20)),
        ),
        st.text(max_size=60),
    )
    def test_whatever_a_model_writes_what_a_skill_is_given_is_what_was_said(
        self, given_values: dict[str, Any], message: str
    ) -> None:
        checked = check_values(MEETING, given_values, said=message)

        for name in ("day", "at", "length"):
            if name in checked.values:
                spec = MEETING.parameters[name]
                assert convert(spec, given_values[name], message) == checked.values[name]
        assert not set(checked.values) & set(checked.unsaid)
        assert not set(checked.invalid) & set(checked.unsaid)
