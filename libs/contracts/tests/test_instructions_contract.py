"""The plan a model writes for a skill that has no script: what it can hold, and what it cannot."""

from typing import Any

import pytest
from pydantic import ValidationError

from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import SkillId
from factotum.contracts.instructions import (
    MAX_BODY_CHARS,
    MAX_HEADERS,
    MAX_PLAN_CHARS,
    MAX_STEPS,
    MAX_URL_CHARS,
    InstructionPlan,
    PlanStep,
)
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import SkillMode
from factotum.contracts.testing import make_tenant_id


def request(**fields: Any) -> dict[str, Any]:
    return {"id": "lamp", "kind": "http", "method": "POST", "url": "http://a.example/x", **fields}


def command(**fields: Any) -> dict[str, Any]:
    return {
        "id": "louder",
        "kind": "command",
        "name": "set_volume",
        "args": {"level": 40},
        **fields,
    }


class TestARequest:
    def test_it_has_a_method_and_an_address(self) -> None:
        step = PlanStep.model_validate(request(headers={"Accept": "application/json"}))

        assert (step.kind, step.method, step.url) == ("http", "POST", "http://a.example/x")
        assert step.headers == {"Accept": "application/json"}

    def test_what_a_model_leaves_out_of_a_step_it_may_write_as_nothing(self) -> None:
        step = PlanStep.model_validate(request(name=None, args={}, headers={}, body=None))

        assert (step.name, step.args, step.headers, step.body) == (None, {}, {}, None)

    @pytest.mark.parametrize("method", ["get", "HEAD", "OPTIONS", "TRACE", "CONNECT", "", "POST "])
    def test_a_method_that_is_not_one_of_the_five_is_refused(self, method: str) -> None:
        with pytest.raises(ValidationError):
            PlanStep.model_validate(request(method=method))

    @pytest.mark.parametrize("missing", ["method", "url"])
    def test_a_request_without_a_method_or_an_address_is_not_one(self, missing: str) -> None:
        fields = request()
        del fields[missing]

        with pytest.raises(ValidationError, match="a method and an address"):
            PlanStep.model_validate(fields)

    def test_a_request_is_not_a_command_too(self) -> None:
        with pytest.raises(ValidationError, match="no command"):
            PlanStep.model_validate(request(name="speak"))
        with pytest.raises(ValidationError, match="no command"):
            PlanStep.model_validate(request(args={"text": "x"}))

    def test_a_body_is_json_or_text(self) -> None:
        assert PlanStep.model_validate(request(body={"a": [1, 2.5, None, "x"]})).body == {
            "a": [1, 2.5, None, "x"]
        }
        assert PlanStep.model_validate(request(body="plain words")).body == "plain words"

    def test_a_body_is_not_a_book(self) -> None:
        PlanStep.model_validate(request(body="x" * MAX_BODY_CHARS))
        with pytest.raises(ValidationError, match="at most"):
            PlanStep.model_validate(request(body="x" * (MAX_BODY_CHARS + 1)))
        with pytest.raises(ValidationError, match="at most"):
            PlanStep.model_validate(request(body={"a": "x" * MAX_BODY_CHARS}))

    def test_a_body_that_is_not_json_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="JSON, or text"):
            PlanStep.model_validate(request(body={"a": object()}))

    def test_an_address_is_not_a_page(self) -> None:
        PlanStep.model_validate(request(url="http://a.example/" + "x" * (MAX_URL_CHARS - 20)))
        with pytest.raises(ValidationError):
            PlanStep.model_validate(request(url="http://a.example/" + "x" * MAX_URL_CHARS))

    def test_a_few_headers_and_short_ones(self) -> None:
        PlanStep.model_validate(request(headers={f"X-{n}": "v" for n in range(MAX_HEADERS)}))
        with pytest.raises(ValidationError):
            PlanStep.model_validate(
                request(headers={f"X-{n}": "v" for n in range(MAX_HEADERS + 1)})
            )
        with pytest.raises(ValidationError, match="short"):
            PlanStep.model_validate(request(headers={"X": "v" * 1001}))
        with pytest.raises(ValidationError, match="short"):
            PlanStep.model_validate(request(headers={"X" * 101: "v"}))


class TestACommand:
    def test_it_has_a_name_and_what_the_command_takes(self) -> None:
        step = PlanStep.model_validate(command())

        assert (step.kind, step.name, step.args) == ("command", "set_volume", {"level": 40})

    def test_a_command_without_a_name_is_not_one(self) -> None:
        with pytest.raises(ValidationError, match="a name"):
            PlanStep.model_validate(command(name=None))

    @pytest.mark.parametrize(
        "extra",
        [{"method": "GET"}, {"url": "http://a.example/"}, {"headers": {"A": "b"}}, {"body": "x"}],
    )
    def test_a_command_is_not_a_request_too(self, extra: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="no request"):
            PlanStep.model_validate(command(**extra))


class TestTheNameOfAStep:
    @pytest.mark.parametrize("name", ["a", "geo", "step_2", "x" + "y" * 23])
    def test_a_short_name_in_lower_case_is_fine(self, name: str) -> None:
        assert PlanStep.model_validate(request(id=name)).id == name

    @pytest.mark.parametrize("name", ["", "Geo", "2geo", "a-b", "a b", "a.b", "x" * 25, "é"])
    def test_anything_else_is_refused(self, name: str) -> None:
        with pytest.raises(ValidationError, match="short name"):
            PlanStep.model_validate(request(id=name))


class TestAPlan:
    def test_a_few_steps_in_order_and_what_to_say(self) -> None:
        plan = InstructionPlan.model_validate(
            {
                "reason": "turns the lamp on and tells",
                "steps": [request(), command()],
                "say": "The lamp is on.",
            }
        )

        assert [step.id for step in plan.steps] == ["lamp", "louder"]
        assert plan.say == "The lamp is on."

    def test_it_may_have_no_steps_and_say_why(self) -> None:
        plan = InstructionPlan.model_validate({"reason": "the instructions cannot be followed"})

        assert (plan.steps, plan.say) == ((), None)

    def test_no_more_than_a_few_steps(self) -> None:
        many = [request(id=f"s{number}") for number in range(MAX_STEPS)]
        InstructionPlan.model_validate({"reason": "x", "steps": many})
        with pytest.raises(ValidationError):
            InstructionPlan.model_validate(
                {"reason": "x", "steps": [*many, request(id="one_too_many")]}
            )

    def test_every_step_has_a_name_of_its_own(self) -> None:
        with pytest.raises(ValidationError, match="name of its own"):
            InstructionPlan.model_validate({"reason": "x", "steps": [request(), request()]})

    def test_a_plan_is_not_a_book(self) -> None:
        steps = [request(id=f"s{n}", body="x" * MAX_BODY_CHARS) for n in range(3)]
        assert len(str(steps)) > MAX_PLAN_CHARS

        with pytest.raises(ValidationError, match="at most"):
            InstructionPlan.model_validate({"reason": "x", "steps": steps})

    def test_it_says_no_more_than_a_sentence_or_two(self) -> None:
        with pytest.raises(ValidationError):
            InstructionPlan.model_validate({"reason": "x", "say": "x" * 601})

    def test_a_field_it_does_not_have_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="explanation"):
            InstructionPlan.model_validate({"reason": "x", "explanation": "y"})

    def test_a_plan_is_the_same_when_it_is_written_out_and_read_back(self) -> None:
        plan = InstructionPlan.model_validate(
            {"reason": "x", "steps": [request(body={"a": 1}), command()], "say": "Done."}
        )

        assert InstructionPlan.model_validate_json(plan.model_dump_json()) == plan


class TestARunOfAPlan:
    def fields(self, **changes: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "tenant_id": make_tenant_id(),
            "owner": Owner(kind=OwnerKind.TENANT, id=make_tenant_id()),
            "skill_id": SkillId(make_tenant_id()),
            "skill_name": "scene",
            "version": 1,
            "mode": SkillMode.INSTRUCTIONS,
            "timeout_s": 10,
        }
        return {**base, **changes}

    def test_a_plan_is_run_for_a_skill_in_instructions_mode(self) -> None:
        plan = InstructionPlan.model_validate({"reason": "x", "steps": [command()]})

        run = RunRequest.model_validate(self.fields(plan=plan))

        assert run.plan == plan

    def test_a_plan_is_not_run_for_a_skill_that_has_a_script(self) -> None:
        plan = InstructionPlan.model_validate({"reason": "x", "steps": [command()]})

        with pytest.raises(ValidationError, match="instructions mode"):
            RunRequest.model_validate(
                self.fields(plan=plan, mode=SkillMode.SCRIPT, entrypoint="scripts/run.py")
            )

    def test_a_run_needs_no_plan_at_all(self) -> None:
        assert RunRequest.model_validate(self.fields()).plan is None
