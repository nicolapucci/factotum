"""The formats a skill declares for a value that is said in words (ADR 0030): what a pattern may
say, what fits which type of parameter, and how a manifest writes them."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from factotum.contracts.formats import (
    DATE_TOKENS,
    TIME_TOKENS,
    FormatGroup,
    FormatKind,
    ValueFormat,
    pattern_parts,
    problem_with_format,
)
from factotum.contracts.skills import ParameterSpec, SkillManifest


def fmt(kind: str, pattern: str) -> ValueFormat:
    return ValueFormat(kind=FormatKind(kind), pattern=pattern)


class TestThePartsOfAPattern:
    def test_tokens_and_the_separators_between_them(self) -> None:
        assert pattern_parts("HH:mm", TIME_TOKENS) == (("HH", True), (":", False), ("mm", True))

    def test_the_longest_token_is_the_one_that_is_meant(self) -> None:
        assert pattern_parts("yyyy-M-d", DATE_TOKENS) == (
            ("yyyy", True),
            ("-", False),
            ("M", True),
            ("-", False),
            ("d", True),
        )
        assert pattern_parts("HHH", TIME_TOKENS) == (("HH", True), ("H", True))

    def test_a_separator_of_two_characters_is_one_part(self) -> None:
        assert pattern_parts("hh: mm a", TIME_TOKENS) == (
            ("hh", True),
            (": ", False),
            ("mm", True),
            (" ", False),
            ("a", True),
        )

    @pytest.mark.parametrize(
        "pattern", ["HH:mx", "HH:mm x", "HH:mm5", "HH'h'mm", "H{mm}", "HH:mm\n", "HH:mm€"]
    )
    def test_a_character_that_is_no_token_and_no_separator_is_refused(self, pattern: str) -> None:
        with pytest.raises(ValueError, match="is not part of the pattern"):
            pattern_parts(pattern, TIME_TOKENS)

    def test_the_case_of_a_token_matters(self) -> None:
        with pytest.raises(ValueError, match="is not part of the pattern"):
            pattern_parts("hh:MM", TIME_TOKENS)  # MM is a month of a date, not the minutes

    @given(
        st.lists(
            st.one_of(
                st.sampled_from(TIME_TOKENS), st.text(alphabet=" :/.,-_+", min_size=1, max_size=3)
            ),
            min_size=1,
            max_size=8,
        )
    )
    def test_the_parts_of_any_pattern_make_it_again(self, pieces: list[str]) -> None:
        pattern = "".join(pieces)
        # two tokens side by side may read as a longer one (h and h are hh): the parts are those
        # of the pattern as written, whatever they are, and they make it up again
        parts = pattern_parts(pattern, TIME_TOKENS)

        assert "".join(text for text, _ in parts) == pattern
        assert all(is_token == (text in TIME_TOKENS) for text, is_token in parts)


class TestWhatFitsAType:
    @pytest.mark.parametrize(
        ("kind", "pattern", "param_type"),
        [
            ("time", "HH:mm", "string"),
            ("time", "H.mm", "string"),
            ("time", "hh:mm a", "string"),
            ("time", "h:mm a", "string"),
            ("duration", "seconds", "integer"),
            ("duration", "minutes", "number"),
            ("duration", "hours", "number"),
            ("duration", "iso", "string"),
            ("date", "dd/MM/yy", "string"),
            ("date", "yyyy-MM-dd", "string"),
            ("date", "d/M", "string"),
            ("in_days", "{n}", "integer"),
            ("in_days", "+{n}d", "string"),
            ("in_days", "tra {n} giorni", "string"),
            ("weekday", "name", "string"),
            ("weekday", "iso", "integer"),
        ],
    )
    def test_the_formats_that_are_right(self, kind: str, pattern: str, param_type: str) -> None:
        assert problem_with_format(fmt(kind, pattern), param_type) is None

    @pytest.mark.parametrize(
        ("kind", "pattern", "param_type", "why"),
        [
            ("time", "HH:mm", "integer", "written as text"),
            ("time", "HH", "string", "the minutes"),
            ("time", "mm", "string", "the hour"),
            ("time", "HH:HH:mm", "string", "the hour"),
            ("time", "mm:HH", "string", "comes before"),
            ("time", "hh:mm", "string", "a 12-hour clock"),
            ("time", "HH:mm a", "string", "a 24-hour one without"),
            ("time", "hh:mm a a", "string", "once"),
            ("time", "HH:ss", "string", "is not part of the pattern"),
            ("duration", "days", "integer", "seconds, minutes, hours or iso"),
            ("duration", "seconds", "string", "is a number"),
            ("duration", "iso", "integer", "is text"),
            ("date", "dd/MM/yy", "integer", "written as text"),
            ("date", "MM/yy", "string", "day of the month"),
            ("date", "dd/yy", "string", "month"),
            ("date", "dd/MM/yy/yyyy", "string", "at most once"),
            ("date", "dd/MM/yy HH", "string", "is not part of the pattern"),
            ("in_days", "+d", "string", "written once"),
            ("in_days", "{n}{n}", "string", "written once"),
            ("in_days", "{n} {m}", "string", "braces"),
            ("in_days", "+{n}d", "integer", "{n} alone"),
            ("in_days", "{n}", "number", "text or a whole number"),
            ("weekday", "name", "integer", "text"),
            ("weekday", "iso", "string", "whole number"),
            ("weekday", "short", "string", "name or iso"),
        ],
    )
    def test_the_formats_that_are_wrong_say_why(
        self, kind: str, pattern: str, param_type: str, why: str
    ) -> None:
        problem = problem_with_format(fmt(kind, pattern), param_type)

        assert problem is not None
        assert why in problem

    def test_every_kind_is_of_one_group(self) -> None:
        assert {kind: fmt(kind.value, "x").group for kind in FormatKind} == {
            FormatKind.TIME: FormatGroup.TIME,
            FormatKind.DURATION: FormatGroup.DURATION,
            FormatKind.DATE: FormatGroup.DAY,
            FormatKind.IN_DAYS: FormatGroup.DAY,
            FormatKind.WEEKDAY: FormatGroup.DAY,
        }


class TestInAParameter:
    def spec(self, **fields: Any) -> ParameterSpec:
        return ParameterSpec.model_validate({"type": "string", **fields})

    def test_a_parameter_has_no_formats_unless_it_declares_them(self) -> None:
        assert self.spec().formats is None

    def test_a_format_is_written_as_its_kind_and_its_pattern(self) -> None:
        spec = self.spec(formats=[{"time": "HH:mm"}])

        assert spec.formats == (fmt("time", "HH:mm"),)

    def test_more_than_one_is_a_parameter_that_takes_the_same_thing_in_more_ways(self) -> None:
        spec = self.spec(formats=[{"date": "dd/MM/yy"}, {"in_days": "+{n}d"}, {"weekday": "name"}])

        assert [f.kind.value for f in spec.formats or ()] == ["date", "in_days", "weekday"]

    def test_the_order_is_the_order_of_what_is_preferred(self) -> None:
        a = self.spec(formats=[{"in_days": "+{n}d"}, {"date": "dd/MM/yy"}])

        assert a.formats is not None
        assert a.formats[0].kind is FormatKind.IN_DAYS

    def test_what_is_written_back_in_full_is_read_as_it_is(self) -> None:
        spec = self.spec(formats=[{"time": "HH:mm"}])

        again = ParameterSpec.model_validate(spec.model_dump(mode="json"))

        assert again == spec

    def test_a_pattern_that_is_wrong_for_the_type_stops_the_parameter(self) -> None:
        with pytest.raises(ValidationError, match=r"format duration 'seconds'.*is a number"):
            self.spec(type="string", formats=[{"duration": "seconds"}])

    def test_two_formats_of_one_kind_are_one_too_many(self) -> None:
        with pytest.raises(ValidationError, match="declared once"):
            self.spec(formats=[{"date": "dd/MM/yy"}, {"date": "yyyy-MM-dd"}])

    def test_a_time_and_a_day_are_not_ways_of_writing_the_same_words(self) -> None:
        with pytest.raises(ValidationError, match="not a mix"):
            self.spec(formats=[{"time": "HH:mm"}, {"date": "dd/MM/yy"}])

    def test_formats_and_a_list_of_choices_are_not_both(self) -> None:
        with pytest.raises(ValidationError, match="not a list of choices"):
            self.spec(enum=["a", "b"], formats=[{"time": "HH:mm"}])

    def test_a_format_with_no_pattern_or_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="Field required"):
            self.spec(formats=[{"kind": "time"}])
        with pytest.raises(ValidationError, match="Field required"):
            self.spec(formats=[{"pattern": "HH:mm"}])
        with pytest.raises(ValidationError, match="hour"):
            self.spec(formats=[{"hour": "HH"}])
        with pytest.raises(ValidationError):
            self.spec(formats=[{"time": "HH:mm", "date": "dd/MM/yy"}])

    def test_the_formats_are_a_short_list_that_is_not_empty(self) -> None:
        with pytest.raises(ValidationError):
            self.spec(formats=[])

    def test_there_are_as_many_formats_as_there_are_kinds_at_most(self) -> None:
        six = [{"time": "HH:mm"}] * 6

        with pytest.raises(ValidationError, match="at most 5"):
            self.spec(formats=six)

    def test_a_pattern_is_short(self) -> None:
        with pytest.raises(ValidationError):
            self.spec(formats=[{"in_days": "x" * 41 + "{n}"}])

    def test_the_json_schema_says_the_formats_and_only_when_there_are_some(self) -> None:
        assert "x-formats" not in self.spec().to_json_schema()
        assert self.spec(formats=[{"time": "HH:mm"}]).to_json_schema()["x-formats"] == [
            {"kind": "time", "pattern": "HH:mm"}
        ]


class TestInAManifest:
    def manifest(self, **fields: Any) -> SkillManifest:
        return SkillManifest.model_validate(
            {
                "name": "meeting-add",
                "description": "Add a meeting.",
                "effect": "reversible",
                **fields,
            }
        )

    def test_a_skill_takes_a_time_in_the_form_it_declares(self) -> None:
        skill = self.manifest(
            parameters={"at": {"type": "string", "formats": [{"time": "HH:mm"}]}},
            required=["at"],
        )

        assert skill.parameters["at"].formats == (fmt("time", "HH:mm"),)

    def test_a_device_is_set_up_and_not_spoken_to_so_its_parameters_have_no_formats(self) -> None:
        with pytest.raises(ValidationError, match="have no formats"):
            self.manifest(
                target="external",
                device_parameters={"at": {"type": "string", "formats": [{"time": "HH:mm"}]}},
            )
