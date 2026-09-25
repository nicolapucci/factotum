"""The language of plans: what a placeholder is, and what it is worth once a plan is run."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.instructions.executor import (
    Context,
    LanguageError,
    Ref,
    StepResult,
    as_text,
    find_refs,
    names_of,
    parse_ref,
    split_url,
    strings_of,
    substitute_json,
    substitute_text,
)


def context(**changes: Any) -> Context:
    fields: dict[str, Any] = {
        "secrets": {"HA_TOKEN": "s3cr3t-token"},
        "device": {"entity_id": "light.living_room", "host": "ha.example"},
        "values": {"brightness": 40, "state": "on", "place": "São Paulo", "flag": True},
    }
    made = Context(**{**fields, **changes})
    made.steps["geo"] = StepResult(
        200,
        '{"results": [{"name": "Rome", "latitude": 41.9, "longitude": 12.5}], "count": 1}',
        {"results": [{"name": "Rome", "latitude": 41.9, "longitude": 12.5}], "count": 1},
        True,
    )
    made.steps["page"] = StepResult(200, "hello", None, False)
    return made


class TestReadingAPlaceholder:
    @pytest.mark.parametrize(
        ("inner", "expected"),
        [
            ("secret.HA_TOKEN", Ref("secret", "HA_TOKEN", (), None, "${secret.HA_TOKEN}")),
            ("device.entity_id", Ref("device", "entity_id", (), None, "${device.entity_id}")),
            ("value.brightness", Ref("value", "brightness", (), None, "${value.brightness}")),
            (
                "value.brightness|round",
                Ref("value", "brightness", (), "round", "${value.brightness|round}"),
            ),
            ("step.geo.status", Ref("step", "geo", ("status",), None, "${step.geo.status}")),
            ("step.geo.text", Ref("step", "geo", ("text",), None, "${step.geo.text}")),
            ("step.geo.json", Ref("step", "geo", ("json",), None, "${step.geo.json}")),
            (
                "step.geo.json.results.0.latitude|round",
                Ref(
                    "step",
                    "geo",
                    ("json", "results", "0", "latitude"),
                    "round",
                    "${step.geo.json.results.0.latitude|round}",
                ),
            ),
            (
                "step.a_1.json.a-b.C_d",
                Ref("step", "a_1", ("json", "a-b", "C_d"), None, "${step.a_1.json.a-b.C_d}"),
            ),
        ],
    )
    def test_a_placeholder_says_what_it_refers_to(self, inner: str, expected: Ref) -> None:
        assert parse_ref(inner) == expected

    @pytest.mark.parametrize(
        "inner",
        [
            "",
            "secret",
            "secret.",
            "secret.lower",
            "secret.A.B",
            "secret.1A",
            "device.Upper",
            "device.a.b",
            "value.1x",
            "value.a.b",
            "value.",
            "step",
            "step.a",
            "step.a.b",
            "step.A.json",
            "step.geo.status.x",
            "step.geo.text.x",
            "step.geo.json..x",
            "step.geo.json.x y",
            "step.geo.json.x/y",
            "step.geo.json.x$",
            "nothing.x",
            "SECRET.HA_TOKEN",
            "value.x|floor",
            "value.x|",
            "value.x|round|round",
            "value.x |round",
            "step.geo.json.é",
        ],
    )
    def test_anything_else_is_not_one(self, inner: str) -> None:
        with pytest.raises(LanguageError):
            parse_ref(inner)

    def test_the_error_says_which_placeholder_and_is_short(self) -> None:
        with pytest.raises(LanguageError) as raised:
            parse_ref("value." + "x" * 500 + ".y")

        assert len(str(raised.value)) < 200


class TestFindingPlaceholders:
    def test_every_placeholder_of_a_text_in_order(self) -> None:
        found = find_refs("a ${value.state} b ${secret.HA_TOKEN} c ${step.geo.json.count}")

        assert [ref.raw for ref in found] == [
            "${value.state}",
            "${secret.HA_TOKEN}",
            "${step.geo.json.count}",
        ]

    def test_a_text_without_any_has_none(self) -> None:
        assert find_refs("no placeholders here, $ and { and } alone are text") == []

    @pytest.mark.parametrize(
        "text",
        [
            "${value.state",
            "a ${ b",
            "${value.state}${",
            "${step.geo.json.${value.x}}",
            "${}",
            "${a}",
        ],
    )
    def test_one_that_is_not_closed_or_not_a_placeholder_is_an_error(self, text: str) -> None:
        with pytest.raises(LanguageError):
            find_refs(text)

    def test_a_dollar_in_front_of_a_placeholder_is_a_dollar(self) -> None:
        assert [ref.raw for ref in find_refs("$${value.state}")] == ["${value.state}"]


class TestWhatAJsonValueHolds:
    def test_every_text_it_has_as_a_value_at_any_depth(self) -> None:
        value = {"a": "1", "b": ["2", {"c": "3", "d": 4}], "e": None, "f": True}

        assert sorted(strings_of(value)) == ["1", "2", "3"]

    def test_a_text_is_a_text(self) -> None:
        assert list(strings_of("x")) == ["x"]
        assert list(strings_of(5)) == []

    def test_every_name_of_a_field_at_any_depth(self) -> None:
        assert sorted(names_of({"a": {"b": [{"c": 1}]}, "d": "x"})) == ["a", "b", "c", "d"]


class TestWhatAPlaceholderIsWorth:
    def test_a_value_keeps_its_type(self) -> None:
        held = context()

        assert held.lookup(parse_ref("value.brightness")) == 40
        assert held.lookup(parse_ref("value.flag")) is True
        assert held.lookup(parse_ref("value.state")) == "on"

    def test_a_value_of_the_device(self) -> None:
        assert context().lookup(parse_ref("device.entity_id")) == "light.living_room"

    def test_a_secret(self) -> None:
        assert context().lookup(parse_ref("secret.HA_TOKEN")) == "s3cr3t-token"

    def test_a_secret_is_not_for_saying(self) -> None:
        with pytest.raises(LanguageError, match="not for saying"):
            context().without_secrets().lookup(parse_ref("secret.HA_TOKEN"))

    def test_without_secrets_what_is_not_a_secret_is_still_there(self) -> None:
        held = context().without_secrets()

        assert held.lookup(parse_ref("value.state")) == "on"
        assert held.lookup(parse_ref("step.geo.json.count")) == 1

    @pytest.mark.parametrize(
        ("inner", "what"),
        [
            ("secret.OTHER", "the secret OTHER is not there"),
            ("device.nothing", "no device called nothing"),
            ("value.nothing", "no value called nothing"),
            ("step.nothing.status", "the step nothing has not been done"),
        ],
    )
    def test_what_is_not_there_is_an_error_that_says_which(self, inner: str, what: str) -> None:
        with pytest.raises(LanguageError, match=what):
            context().lookup(parse_ref(inner))

    def test_the_status_and_the_text_of_a_step(self) -> None:
        held = context()

        assert held.lookup(parse_ref("step.geo.status")) == 200
        assert held.lookup(parse_ref("step.page.text")) == "hello"

    def test_a_field_of_the_json_of_a_step(self) -> None:
        held = context()

        assert held.lookup(parse_ref("step.geo.json.results.0.latitude")) == 41.9
        assert held.lookup(parse_ref("step.geo.json.results.0.name")) == "Rome"
        assert held.lookup(parse_ref("step.geo.json.count")) == 1
        assert held.lookup(parse_ref("step.geo.json.results.0")) == {
            "name": "Rome",
            "latitude": 41.9,
            "longitude": 12.5,
        }

    @pytest.mark.parametrize(
        "inner",
        [
            "step.geo.json.nothing",
            "step.geo.json.results.1",
            "step.geo.json.results.name",
            "step.geo.json.count.deeper",
            "step.geo.json.results.0.latitude.deeper",
            "step.page.json.anything",
        ],
    )
    def test_a_path_that_leads_nowhere_is_an_error(self, inner: str) -> None:
        with pytest.raises(LanguageError):
            context().lookup(parse_ref(inner))

    @pytest.mark.parametrize(
        ("number", "rounded"), [(28.4, 28), (28.5, 29), (28.6, 29), (2.5, 3), (-1.5, -1), (7, 7)]
    )
    def test_a_number_is_rounded_as_a_person_rounds_it(self, number: float, rounded: int) -> None:
        held = context(values={"n": number})

        worth = held.lookup(parse_ref("value.n|round"))

        assert worth == rounded
        assert isinstance(worth, int)

    @pytest.mark.parametrize("value", ["twenty", True, None, [1], {"a": 1}])
    def test_only_a_number_is_rounded(self, value: Any) -> None:
        with pytest.raises(LanguageError, match="not a number"):
            context(values={"n": value}).lookup(parse_ref("value.n|round"))


class TestWrittenIntoAText:
    @pytest.mark.parametrize(
        ("value", "written"),
        [
            ("words", "words"),
            (40, "40"),
            (2.5, "2.5"),
            (True, "true"),
            (False, "false"),
            ({"a": [1, "x"]}, '{"a":[1,"x"]}'),
            ([1, 2], "[1,2]"),
            ("é", "é"),
        ],
    )
    def test_a_value_as_it_is_written(self, value: Any, written: str) -> None:
        assert as_text(value) == written

    def test_nothing_is_not_written(self) -> None:
        with pytest.raises(LanguageError, match="nothing"):
            as_text(None)

    def test_a_text_has_its_placeholders_put_in(self) -> None:
        text = substitute_text(
            "Bearer ${secret.HA_TOKEN} for ${value.state}, ${value.brightness}%", context()
        )

        assert text == "Bearer s3cr3t-token for on, 40%"

    def test_a_text_without_any_is_as_it_was(self) -> None:
        assert substitute_text("nothing to put in $ { }", context()) == "nothing to put in $ { }"

    def test_in_an_address_what_is_put_in_cannot_end_the_part_it_is_in(self) -> None:
        text = substitute_text(
            "http://a.example/find?name=${value.place}&x=1", context(), quote=True
        )

        assert text == "http://a.example/find?name=S%C3%A3o%20Paulo&x=1"

    @pytest.mark.parametrize("value", ["a&b=c", "a#b", "a/b", "a b", "a?b", "a%20b", "é;"])
    def test_a_value_in_an_address_stays_in_its_place(self, value: str) -> None:
        text = substitute_text(
            "http://a.example/p?q=${value.v}", context(values={"v": value}), quote=True
        )

        head, _, query = text.partition("?")
        assert head == "http://a.example/p"
        assert query.startswith("q=")
        assert not any(mark in query[2:] for mark in "&#/? =;")

    @given(value=st.text(max_size=40))
    def test_whatever_is_put_in_an_address_stays_in_its_place(self, value: str) -> None:
        text = substitute_text(
            "http://a.example/p?q=${value.v}", context(values={"v": value}), quote=True
        )

        assert text.count("?") == 1
        assert "&" not in text
        assert "#" not in text
        assert " " not in text

    def test_a_placeholder_that_is_not_there_is_an_error(self) -> None:
        with pytest.raises(LanguageError):
            substitute_text("${value.nothing}", context())

    def test_a_placeholder_that_is_not_one_is_an_error(self) -> None:
        with pytest.raises(LanguageError):
            substitute_text("${nothing.x}", context())


class TestWrittenIntoJson:
    def test_a_text_that_is_one_placeholder_becomes_the_value_with_its_type(self) -> None:
        held = context()

        assert substitute_json("${value.brightness}", held) == 40
        assert substitute_json("${value.flag}", held) is True
        assert substitute_json("${step.geo.json.results.0}", held) == {
            "name": "Rome",
            "latitude": 41.9,
            "longitude": 12.5,
        }

    def test_a_placeholder_in_more_text_is_written_as_text(self) -> None:
        assert substitute_json("level ${value.brightness}%", context()) == "level 40%"

    def test_it_goes_into_lists_and_objects_at_any_depth(self) -> None:
        body = {
            "entity_id": "${device.entity_id}",
            "data": [{"brightness_pct": "${value.brightness}", "label": "on ${value.state}"}],
            "count": 3,
            "flag": False,
            "none": None,
        }

        assert substitute_json(body, context()) == {
            "entity_id": "light.living_room",
            "data": [{"brightness_pct": 40, "label": "on on"}],
            "count": 3,
            "flag": False,
            "none": None,
        }

    def test_the_names_of_fields_are_left_as_they_are(self) -> None:
        assert substitute_json({"${value.state}": 1}, context()) == {"${value.state}": 1}

    def test_a_placeholder_that_is_not_there_is_an_error(self) -> None:
        with pytest.raises(LanguageError):
            substitute_json({"a": "${value.nothing}"}, context())


class TestTheAuthorityOfAnAddress:
    @pytest.mark.parametrize(
        ("url", "parts"),
        [
            ("http://ha.example:8123/api/x?y=1", ("http", "ha.example:8123", "/api/x?y=1")),
            ("https://a.example", ("https", "a.example", "")),
            ("https://a.example?x=1", ("https", "a.example", "?x=1")),
            ("http://u:p@a.example/x", ("http", "u:p@a.example", "/x")),
            ("HTTP://${device.host}/x", ("HTTP", "${device.host}", "/x")),
            ("http:///x", ("http", "", "/x")),
        ],
    )
    def test_the_scheme_the_authority_and_the_rest(
        self, url: str, parts: tuple[str, str, str]
    ) -> None:
        assert split_url(url) == parts

    @pytest.mark.parametrize(
        "url", ["", "a.example/x", "//a.example/x", "http:/a.example", "1http://a"]
    )
    def test_what_is_not_an_address_has_none(self, url: str) -> None:
        assert split_url(url) is None
