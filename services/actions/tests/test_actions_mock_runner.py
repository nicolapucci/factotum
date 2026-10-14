"""The runner that answers the way a skill would and does nothing (development): what a
``mock.yaml`` may say, which reply fits what the skill was called with, and what is answered in
each language."""

from typing import Any

import pytest

from factotum.actions.execution import DryRunRunner
from factotum.actions.mock_runner import (
    ANY_VALUE,
    MAX_MOCK_BYTES,
    MOCK_FILE,
    MockAnswers,
    MockReply,
    MockRunner,
    blanks_of,
    read_mock,
)
from factotum.actions.skills import LoadedSkill, parse_skill
from factotum.actions.store import InMemoryStore
from factotum.actions.testing import START
from factotum.contracts.effects import Effect
from factotum.contracts.errors import SkillRunError
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import TenantId
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import ParamValue, SkillMode, SkillPermissions
from factotum.contracts.testing import make_tenant_id

THERMOSTAT = """\
replies:
  - when: {room: "*", temperature: "*"}
    it: "Termostato di {room} a {temperature} gradi."
    en: "The {room} thermostat is at {temperature} degrees."
  - when: {room: "*"}
    it: "Termostato di {room}."
    en: "The {room} thermostat."
  - it: "Termostato a {temperature} gradi."
    en: "Thermostat at {temperature} degrees."
"""

SKILL = """\
---
name: thermostat-set
description: Set the temperature of the heating of the home.
parameters:
  temperature: {type: integer, minimum: 5, maximum: 30}
  room: {type: string}
required: [temperature]
effect: reversible
---
A mock.
"""


def answers(text: str = THERMOSTAT) -> MockAnswers:
    return read_mock(text.encode("utf-8"))


class TestWhatAMockSays:
    def test_it_is_replies_in_both_languages_and_the_last_is_for_whatever_is_given(self) -> None:
        mock = answers()

        assert len(mock.replies) == 3
        assert mock.replies[-1].when == {}
        assert mock.replies[0].when == {"room": ANY_VALUE, "temperature": ANY_VALUE}

    @pytest.mark.parametrize(
        ("text", "why"),
        [
            ("", "replies"),
            ("replies: []", "replies"),
            ("- it: x", "replies"),
            ("replies: [{it: x}]", "en"),
            ("replies: [{en: x}]", "it"),
            ("replies: [{it: '', en: x}]", "it"),
            ("replies: [{it: x, en: y, fr: z}]", "fr"),
            ("replies: [{when: {a: '*'}, it: x, en: y}]", "no 'when'"),
            ("replies: [{it: x, en: y}, {when: {a: '*'}, it: x, en: y}]", "no 'when'"),
            ("replies: [{it: '{}', en: y}]", "name"),
            ("replies: [{it: '{0}', en: y}]", "name"),
            ("replies: [{it: '{a.b}', en: y}]", "name"),
            ("replies: [{it: '{a[0]}', en: y}]", "name"),
            ("replies: [{it: x, en: '{a!r}'}]", "name"),
            ("replies: [{it: x, en: '{a:>5}'}]", "name"),
            ("replies: [{it: x, en: y}]\nextra: 1", "extra"),
            ("{{{{", "not YAML"),
        ],
    )
    def test_what_is_not_a_mock_is_refused_and_said_by_what_is_wrong(
        self, text: str, why: str
    ) -> None:
        with pytest.raises(SkillRunError, match=why):
            read_mock(text.encode("utf-8"))

    def test_a_file_that_is_too_big_is_refused(self) -> None:
        with pytest.raises(SkillRunError, match="bytes"):
            read_mock(b" " * (MAX_MOCK_BYTES + 1))

    def test_a_file_that_is_not_text_is_refused(self) -> None:
        with pytest.raises(SkillRunError, match="not YAML"):
            read_mock(b"\xff\xfe\x00")

    def test_what_is_wrong_is_said_and_what_the_file_holds_is_not(self) -> None:
        with pytest.raises(SkillRunError) as raised:
            read_mock(b"replies: [{it: x, en: y, password: hunter2}]")

        assert "hunter2" not in raised.value.message
        assert "password" in raised.value.message

    def test_a_reply_is_not_more_than_a_sentence_or_two(self) -> None:
        long = "x" * 501

        with pytest.raises(SkillRunError):
            read_mock(f"replies: [{{it: {long}, en: y}}]".encode())

    def test_the_blanks_of_a_text_are_its_names(self) -> None:
        assert blanks_of("A {room} at {temperature} degrees, {room}") == {"room", "temperature"}
        assert blanks_of("Fatto.") == frozenset()
        assert blanks_of("{{not a blank}}") == frozenset()

    def test_a_reply_knows_all_the_blanks_it_has_in_both_languages(self) -> None:
        reply = MockReply(it="{a} {b}", en="{c}")

        assert reply.blanks == {"a", "b", "c"}


class TestWhichReplyFits:
    @pytest.mark.parametrize(
        ("params", "said"),
        [
            ({"room": "cucina", "temperature": 21}, "Termostato di cucina a 21 gradi."),
            ({"room": "cucina"}, "Termostato di cucina."),
            ({"temperature": 21}, "Termostato a 21 gradi."),
            ({}, "Termostato a  gradi."),
        ],
    )
    def test_the_first_that_fits_is_the_one_and_the_last_fits_whatever_is_given(
        self, params: dict[str, ParamValue], said: str
    ) -> None:
        assert answers().answer(params).says("it", params) == said

    @pytest.mark.parametrize("nothing", [None, ""])
    def test_a_value_that_is_nothing_was_not_given(self, nothing: str | None) -> None:
        reply = MockReply(when={"room": ANY_VALUE}, it="x", en="y")
        params: dict[str, Any] = {"room": nothing}

        assert not reply.fits(params)

    def test_a_value_that_is_zero_or_false_was_given(self) -> None:
        reply = MockReply(when={"level": ANY_VALUE, "muted": ANY_VALUE}, it="x", en="y")

        assert reply.fits({"level": 0, "muted": False})

    def test_a_value_is_equal_to_what_is_wanted_whatever_its_case(self) -> None:
        reply = MockReply(when={"speed": "high"}, it="x", en="y")

        assert reply.fits({"speed": "HIGH"})
        assert reply.fits({"speed": "high"})
        assert not reply.fits({"speed": "low"})
        assert not reply.fits({})

    def test_a_number_is_equal_to_the_same_number_said_as_text(self) -> None:
        reply = MockReply(when={"channel": 5}, it="x", en="y")

        assert reply.fits({"channel": 5})
        assert reply.fits({"channel": "5"})
        assert not reply.fits({"channel": 6})

    def test_every_condition_has_to_be_met(self) -> None:
        reply = MockReply(when={"a": ANY_VALUE, "b": "yes"}, it="x", en="y")

        assert reply.fits({"a": 1, "b": "yes"})
        assert not reply.fits({"a": 1, "b": "no"})
        assert not reply.fits({"b": "yes"})


class TestSayingIt:
    def test_in_the_language_of_the_person_and_in_english_for_any_other(self) -> None:
        reply = MockReply(it="Ciao {name}.", en="Hello {name}.")
        params = {"name": "Ada"}

        assert reply.says("it", params) == "Ciao Ada."
        assert reply.says("en", params) == "Hello Ada."
        assert reply.says("fr", params) == "Hello Ada."
        assert reply.says(None, params) == "Hello Ada."

    def test_a_value_that_was_not_given_is_nothing_and_never_an_error(self) -> None:
        reply = MockReply(it="a {x} b", en="a {x} b")

        assert reply.says("it", {}) == "a  b"

    def test_each_kind_of_value_is_said_as_text(self) -> None:
        reply = MockReply(it="{n} {f} {t} {s}", en="x")

        assert reply.says("it", {"n": 3, "f": 1.5, "t": True, "s": "cucina"}) == "3 1.5 True cucina"

    def test_what_is_in_a_value_is_not_read_as_a_blank(self) -> None:
        reply = MockReply(it="{x}", en="x")

        assert reply.says("it", {"x": "{y}"}) == "{y}"

    def test_a_brace_that_is_doubled_is_one(self) -> None:
        reply = MockReply(it="{{ok}}", en="x")

        assert reply.says("it", {}) == "{ok}"


def request_of(
    tenant: TenantId, installed: Any, params: dict[str, ParamValue], language: str | None = "it"
) -> RunRequest:
    return RunRequest(
        tenant_id=tenant,
        owner=Owner.of_subject(Subject.tenant(tenant)),
        skill_id=installed.skill_id,
        skill_name="thermostat-set",
        version=installed.version,
        mode=SkillMode.INSTRUCTIONS,
        params=params,
        permissions=SkillPermissions(),
        timeout_s=10,
        language=language,
    )


async def installed_with(store: InMemoryStore, tenant: TenantId, files: dict[str, bytes]) -> Any:
    loaded = parse_skill(SKILL, where="the test", files=files)
    return await store.install(tenant, Owner.of_subject(Subject.tenant(tenant)), loaded, now=START)


class TestTheRunner:
    async def test_a_skill_answers_as_its_mock_says_in_the_language_of_the_person(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        installed = await installed_with(store, tenant, {MOCK_FILE: THERMOSTAT.encode()})
        runner = MockRunner(store)

        italian = await runner.run(request_of(tenant, installed, {"temperature": 21}, "it"))
        english = await runner.run(request_of(tenant, installed, {"temperature": 21}, "en"))

        assert italian.message == "Termostato a 21 gradi."
        assert english.message == "Thermostat at 21 degrees."
        assert not italian.dry_run
        assert italian.commands == ()

    async def test_the_reply_that_fits_what_it_was_called_with_is_the_one(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        installed = await installed_with(store, tenant, {MOCK_FILE: THERMOSTAT.encode()})

        done = await MockRunner(store).run(
            request_of(tenant, installed, {"temperature": 19, "room": "bedroom"}, "en")
        )

        assert done.message == "The bedroom thermostat is at 19 degrees."

    async def test_a_skill_with_no_mock_is_answered_as_the_dry_run_does(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        installed = await installed_with(store, tenant, {"scripts/run.py": b"x"})
        request = request_of(tenant, installed, {"temperature": 21})

        done = await MockRunner(store).run(request)

        assert done == await DryRunRunner().run(request)
        assert done.dry_run

    async def test_a_skill_whose_files_are_not_there_is_answered_as_the_dry_run_does(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        installed = await installed_with(store, tenant, {MOCK_FILE: THERMOSTAT.encode()})
        request = request_of(make_tenant_id(), installed, {"temperature": 21})  # not its tenant

        done = await MockRunner(store).run(request)

        assert done.dry_run

    async def test_a_mock_that_is_not_one_is_a_skill_that_failed_and_says_what_is_wrong(
        self,
    ) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        installed = await installed_with(store, tenant, {MOCK_FILE: b"replies: []"})

        with pytest.raises(SkillRunError, match="not a mock"):
            await MockRunner(store).run(request_of(tenant, installed, {"temperature": 21}))

    async def test_an_answer_of_nothing_is_no_message(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        silent = b"replies: [{it: '{room}', en: '{room}'}]"
        installed = await installed_with(store, tenant, {MOCK_FILE: silent})

        done = await MockRunner(store).run(request_of(tenant, installed, {"temperature": 21}))

        assert done.message is None
        assert not done.dry_run

    async def test_each_tenant_has_its_own_mock_of_the_same_skill(self) -> None:
        store = InMemoryStore()
        one, two = make_tenant_id(), make_tenant_id()
        mine = await installed_with(store, one, {MOCK_FILE: b"replies: [{it: uno, en: one}]"})
        yours = await installed_with(store, two, {MOCK_FILE: b"replies: [{it: due, en: two}]"})
        runner = MockRunner(store)

        first = await runner.run(request_of(one, mine, {"temperature": 21}))
        second = await runner.run(request_of(two, yours, {"temperature": 21}))

        assert (first.message, second.message) == ("uno", "due")

    async def test_the_files_of_the_version_that_runs_are_the_ones_that_answer(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        first = await installed_with(store, tenant, {MOCK_FILE: b"replies: [{it: uno, en: one}]"})
        second = await installed_with(store, tenant, {MOCK_FILE: b"replies: [{it: due, en: two}]"})
        runner = MockRunner(store)

        was = await runner.run(request_of(tenant, first, {"temperature": 21}))
        is_now = await runner.run(request_of(tenant, second, {"temperature": 21}))

        assert second.version == first.version + 1
        assert (was.message, is_now.message) == ("uno", "due")


def test_a_mock_skill_is_a_skill_like_any_other() -> None:
    loaded: LoadedSkill = parse_skill(SKILL, where="the test", files={MOCK_FILE: b"x"})

    assert loaded.manifest.effect is Effect.REVERSIBLE
    assert loaded.manifest.mode is SkillMode.INSTRUCTIONS  # no script: there is nothing to run
