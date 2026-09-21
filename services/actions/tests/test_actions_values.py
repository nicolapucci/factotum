import math
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.values import MAX_TEXT_CHARS, check_values
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


def check(**given: Any) -> Any:
    return check_values(MANIFEST, given)


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
        checked = check_values(MANIFEST, {"state": "on", name: written})

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
    checked = check_values(MANIFEST, given_values)

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
