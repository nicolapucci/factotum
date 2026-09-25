"""The program that runs a plan, against a web server on this machine: what it asks, what it does
with the answers, and what it does when the world does not give what the plan needs."""

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from factotum.actions.instructions import executor
from factotum.actions.instructions.executor import (
    Context,
    StepError,
    StepResult,
    command,
    request,
    run,
)
from factotum.actions.instructions.testing import (
    FakeWeb,
    clean_environment,
    plan_dict,
    step_command,
    step_http,
)

PROGRAM = Path(executor.__file__)


@pytest.fixture(autouse=True)
def _no_proxy_of_the_developer(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def web() -> Any:
    with FakeWeb() as server:
        yield server


def held(**changes: Any) -> Context:
    fields: dict[str, Any] = {
        "secrets": {"HA_TOKEN": "s3cr3t-token"},
        "device": {"entity_id": "light.living_room"},
        "values": {"brightness": 40, "place": "Rome", "text": "Dinner is ready"},
    }
    return Context(**{**fields, **changes})


class TestARequest:
    def test_a_get_is_made_and_what_comes_back_is_read(self, web: FakeWeb) -> None:
        web.answer("GET", "/v1/find", json_body={"results": [{"latitude": 41.9}]})

        got = request(step_http("geo", "GET", web.url("/v1/find?count=1")), held(), 5)

        assert got.status == 200
        assert got.document == {"results": [{"latitude": 41.9}]}
        assert got.has_document
        [seen] = web.seen
        assert (seen.method, seen.target) == ("GET", "/v1/find?count=1")
        assert seen.headers["user-agent"] == "Factotum/1 (instructions)"

    def test_a_json_body_has_its_placeholders_put_in_with_their_types(self, web: FakeWeb) -> None:
        web.answer("POST", "/api/light", json_body=[])
        step = step_http(
            "lamp",
            "POST",
            web.url("/api/light"),
            headers={"Authorization": "Bearer ${secret.HA_TOKEN}"},
            body={"entity_id": "${device.entity_id}", "brightness_pct": "${value.brightness}"},
        )

        request(step, held(), 5)

        [seen] = web.seen
        assert seen.json() == {"entity_id": "light.living_room", "brightness_pct": 40}
        assert seen.headers["authorization"] == "Bearer s3cr3t-token"
        assert seen.headers["content-type"] == "application/json"

    def test_a_text_body_is_sent_as_text(self, web: FakeWeb) -> None:
        web.answer("POST", "/topic", text="ok")

        request(step_http("note", "POST", web.url("/topic"), body="${value.text}!"), held(), 5)

        [seen] = web.seen
        assert seen.body == b"Dinner is ready!"
        assert seen.headers["content-type"] == "text/plain; charset=utf-8"

    def test_what_the_plan_says_of_the_headers_is_not_overruled(self, web: FakeWeb) -> None:
        web.answer("PUT", "/x", text="ok")
        step = step_http(
            "put",
            "PUT",
            web.url("/x"),
            headers={"content-type": "application/vnd.x+json", "User-Agent": "mine/1"},
            body={"a": 1},
        )

        request(step, held(), 5)

        [seen] = web.seen
        assert seen.headers["content-type"] == "application/vnd.x+json"
        assert seen.headers["user-agent"] == "mine/1"

    @pytest.mark.parametrize("method", ["PATCH", "DELETE", "PUT"])
    def test_the_other_methods_are_made_too(self, web: FakeWeb, method: str) -> None:
        web.answer(method, "/x", text="ok")

        request(step_http("s", method, web.url("/x")), held(), 5)

        assert [seen.method for seen in web.seen] == [method]

    def test_what_is_put_into_an_address_is_written_so_that_it_stays_in_its_place(
        self, web: FakeWeb
    ) -> None:
        web.answer("GET", "/find", text="ok")
        held_up = held(values={"place": "São Paulo & co"})

        request(step_http("geo", "GET", web.url("/find?name=${value.place}&count=1")), held_up, 5)

        [seen] = web.seen
        assert seen.query == "name=S%C3%A3o%20Paulo%20%26%20co&count=1"

    def test_a_space_that_the_plan_wrote_is_written_as_one_should_be(self, web: FakeWeb) -> None:
        web.answer("GET", "/find", text="ok")

        request(step_http("geo", "GET", web.url("/find?name=New York")), held(), 5)

        assert web.seen[0].query == "name=New%20York"

    def test_an_answer_that_is_not_json_is_text(self, web: FakeWeb) -> None:
        web.answer("GET", "/page", text="just words")

        got = request(step_http("p", "GET", web.url("/page")), held(), 5)

        assert (got.text, got.has_document) == ("just words", False)

    def test_an_answer_with_no_body_is_an_answer(self, web: FakeWeb) -> None:
        web.answer("POST", "/quiet", status=200, text="")

        got = request(step_http("q", "POST", web.url("/quiet")), held(), 5)

        assert (got.status, got.text, got.has_document) == (200, "", False)

    def test_an_answer_in_another_charset_is_read_in_it(self, web: FakeWeb) -> None:
        web.answer(
            "GET",
            "/latin",
            raw="caffè".encode("latin-1"),
            headers={"Content-Type": "text/plain; charset=latin-1"},
        )

        assert request(step_http("l", "GET", web.url("/latin")), held(), 5).text == "caffè"

    def test_an_answer_in_a_charset_that_is_not_one_is_read_as_text(self, web: FakeWeb) -> None:
        web.answer(
            "GET", "/odd", raw=b"plain", headers={"Content-Type": "text/plain; charset=nonsense"}
        )

        assert request(step_http("o", "GET", web.url("/odd")), held(), 5).text == "plain"


class TestWhenTheWorldDoesNotGiveWhatThePlanNeeds:
    @pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 404, 500, 503])
    def test_an_answer_of_300_or_more_is_an_error_that_says_which(
        self, web: FakeWeb, status: int
    ) -> None:
        web.answer("GET", "/x", status=status, text="no")

        with pytest.raises(StepError, match=f"the answer is {status}"):
            request(step_http("s", "GET", web.url("/x")), held(), 5)

    def test_an_answer_that_says_to_go_elsewhere_is_not_followed(self, web: FakeWeb) -> None:
        web.answer("GET", "/old", status=302, headers={"Location": web.url("/new")})
        web.answer("GET", "/new", text="there")

        with pytest.raises(StepError, match="302"):
            request(step_http("s", "GET", web.url("/old")), held(), 5)

        assert [seen.path for seen in web.seen] == ["/old"]

    def test_an_answer_that_is_too_big_is_an_error(
        self, web: FakeWeb, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(executor, "MAX_RESPONSE_BYTES", 100)
        web.answer("GET", "/big", raw=b"x" * 101)

        with pytest.raises(StepError, match="too big"):
            request(step_http("s", "GET", web.url("/big")), held(), 5)

    def test_an_answer_that_is_just_small_enough_is_read(
        self, web: FakeWeb, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(executor, "MAX_RESPONSE_BYTES", 100)
        web.answer("GET", "/edge", raw=b"x" * 100)

        assert len(request(step_http("s", "GET", web.url("/edge")), held(), 5).text) == 100

    def test_what_is_kept_of_the_text_is_limited(
        self, web: FakeWeb, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(executor, "MAX_TEXT_CHARS", 10)
        web.answer("GET", "/long", raw=b"x" * 50)

        assert request(step_http("s", "GET", web.url("/long")), held(), 5).text == "x" * 10

    def test_a_host_that_is_not_there_is_an_error_that_does_not_say_where(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_: Any, **__: Any) -> None:
            raise urllib.error.URLError(ConnectionRefusedError("127.0.0.1:1 refused"))

        monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)

        with pytest.raises(StepError, match="did not get through") as raised:
            request(step_http("s", "GET", "http://127.0.0.1:1/x"), held(), 5)

        assert "127.0.0.1" not in str(raised.value)
        assert "ConnectionRefusedError" in str(raised.value)

    def test_a_host_that_is_slow_is_given_up_on_at_the_end_of_the_time_that_is_left(
        self, web: FakeWeb
    ) -> None:
        web.answer("GET", "/slow", text="late")
        web.delay_s = 3

        with pytest.raises(StepError, match="did not get through"):
            request(step_http("s", "GET", web.url("/slow")), held(), 0.5)

    def test_a_get_with_a_body_is_not_made(self, web: FakeWeb) -> None:
        with pytest.raises(StepError, match="no body"):
            request(step_http("s", "GET", web.url("/x"), body={"a": 1}), held(), 5)

        assert web.seen == []

    @pytest.mark.parametrize(
        "step",
        [
            {"id": "s", "kind": "http", "method": "TRACE", "url": "http://a/"},
            {"id": "s", "kind": "http", "method": "GET"},
            {"id": "s", "kind": "http", "method": "GET", "url": 5},
        ],
    )
    def test_what_is_not_a_request_is_not_made(self, step: dict[str, Any]) -> None:
        with pytest.raises(StepError, match="not a request"):
            request(step, held(), 5)

    def test_a_header_that_would_split_the_request_is_not_sent(self, web: FakeWeb) -> None:
        step = step_http("s", "GET", web.url("/x"), headers={"X-A": "one\r\nX-B: two"})

        with pytest.raises(StepError, match="did not get through"):
            request(step, held(), 5)

        assert web.seen == []


class TestRunningAPlan:
    def test_the_steps_are_done_in_order_and_each_can_use_what_an_earlier_one_got(
        self, web: FakeWeb
    ) -> None:
        web.answer("GET", "/geo", json_body={"results": [{"latitude": 41.9, "longitude": 12.5}]})
        web.answer("GET", "/forecast", json_body={"current": {"temperature_2m": 27.8}})
        plan = plan_dict(
            step_http("geo", "GET", web.url("/geo?name=${value.place}")),
            step_http(
                "now",
                "GET",
                web.url(
                    "/forecast?latitude=${step.geo.json.results.0.latitude}"
                    "&longitude=${step.geo.json.results.0.longitude}"
                ),
            ),
            say="In ${value.place} it is ${step.now.json.current.temperature_2m|round} degrees.",
        )

        answer = run(plan, context=held(), budget_s=10)

        assert answer["message"] == "In Rome it is 28 degrees."
        assert [seen.target for seen in web.seen] == [
            "/geo?name=Rome",
            "/forecast?latitude=41.9&longitude=12.5",
        ]
        assert answer["data"] == {"steps": {"geo": {"status": 200}, "now": {"status": 200}}}
        assert answer["commands"] == []

    def test_a_command_is_answered_as_data_with_its_placeholders_put_in(self) -> None:
        plan = plan_dict(
            step_command("say", "speak", text="${value.text} at ${value.brightness}"),
            step_command("louder", "set_volume", level="${value.brightness}"),
        )

        answer = run(plan, context=held(), budget_s=10)

        assert answer["commands"] == [
            {"name": "speak", "text": "Dinner is ready at 40"},
            {"name": "set_volume", "level": 40},
        ]
        assert answer["message"] is None

    def test_a_secret_is_never_put_in_a_command(self) -> None:
        plan = plan_dict(step_command("say", "speak", text="${secret.HA_TOKEN}"))

        with pytest.raises(StepError, match="say: a secret is not for saying"):
            run(plan, context=held(), budget_s=10)

    def test_a_secret_is_never_said(self, web: FakeWeb, capsys: pytest.CaptureFixture[str]) -> None:
        web.answer("GET", "/x", text="ok")
        plan = plan_dict(
            step_http("s", "GET", web.url("/x")), say="The token is ${secret.HA_TOKEN}"
        )

        answer = run(plan, context=held(), budget_s=10)

        assert answer["message"] is None
        assert "s3cr3t-token" not in capsys.readouterr().err

    def test_what_is_to_be_said_but_cannot_be_worked_out_is_left_unsaid_and_the_run_stands(
        self, web: FakeWeb, capsys: pytest.CaptureFixture[str]
    ) -> None:
        web.answer("GET", "/x", json_body={"a": 1})
        plan = plan_dict(
            step_http("s", "GET", web.url("/x")), say="It is ${step.s.json.nothing} degrees."
        )

        answer = run(plan, context=held(), budget_s=10)

        assert answer["message"] is None
        assert answer["data"] == {"steps": {"s": {"status": 200}}}
        assert "could not be worked out" in capsys.readouterr().err

    @pytest.mark.parametrize("say", [None, "", "   "])
    def test_no_sentence_is_no_message(self, say: str | None) -> None:
        assert (
            run(plan_dict(step_command("c", "stop"), say=say), context=held(), budget_s=5)[
                "message"
            ]
            is None
        )

    def test_a_sentence_is_trimmed_and_not_too_long(self) -> None:
        plan = plan_dict(step_command("c", "stop"), say="  " + "a" * 3000 + "  ")

        message = run(plan, context=held(), budget_s=5)["message"]

        assert message == "a" * executor.MAX_MESSAGE_CHARS

    def test_the_first_step_that_fails_ends_the_run_and_says_which_it_was(
        self, web: FakeWeb
    ) -> None:
        web.answer("GET", "/one", text="ok")
        web.answer("GET", "/two", status=500)
        plan = plan_dict(
            step_http("one", "GET", web.url("/one")),
            step_http("two", "GET", web.url("/two")),
            step_http("three", "GET", web.url("/three")),
        )

        with pytest.raises(StepError, match="two: the answer is 500"):
            run(plan, context=held(), budget_s=10)

        assert [seen.path for seen in web.seen] == ["/one", "/two"]

    def test_a_value_that_is_not_there_ends_the_run_at_the_step_that_needs_it(
        self, web: FakeWeb
    ) -> None:
        web.answer("GET", "/one", json_body={"a": 1})
        plan = plan_dict(
            step_http("one", "GET", web.url("/one")),
            step_http("two", "GET", web.url("/two/${step.one.json.missing}")),
        )

        with pytest.raises(StepError, match="two: the answer of one has nothing at missing"):
            run(plan, context=held(), budget_s=10)

    def test_a_step_that_comes_later_cannot_be_used_before_it_is_done(self, web: FakeWeb) -> None:
        plan = plan_dict(step_http("one", "GET", web.url("/${step.two.status}")))

        with pytest.raises(StepError, match="the step two has not been done"):
            run(plan, context=held(), budget_s=10)

    def test_the_time_of_the_run_is_the_time_of_all_its_steps(self, web: FakeWeb) -> None:
        web.answer("GET", "/x", text="ok")
        moments = iter([100.0, 101.0, 130.0, 131.0])
        plan = plan_dict(step_http("a", "GET", web.url("/x")), step_http("b", "GET", web.url("/x")))

        with pytest.raises(StepError, match="b: there is no time left"):
            run(plan, context=held(), budget_s=20, now=lambda: next(moments))

        assert len(web.seen) == 1

    @pytest.mark.parametrize(
        "step",
        [{"id": "s", "kind": "shell", "cmd": "ls"}, {"id": "s"}, "not a step", 5, None],
    )
    def test_a_step_that_is_neither_a_request_nor_a_command_is_not_done(self, step: Any) -> None:
        with pytest.raises(StepError, match=r"neither|not a step"):
            run({"steps": [step]}, context=held(), budget_s=5)

    def test_a_plan_whose_steps_are_not_a_list_is_not_run(self) -> None:
        with pytest.raises(StepError, match="no steps"):
            run({"steps": "everything"}, context=held(), budget_s=5)

    def test_a_plan_with_no_steps_answers_what_it_says(self) -> None:
        assert run({"say": "Nothing to do."}, context=held(), budget_s=5) == {
            "message": "Nothing to do.",
            "data": {"steps": {}},
            "commands": [],
        }

    def test_a_command_that_is_not_one_is_not_answered(self) -> None:
        with pytest.raises(StepError, match="not a command"):
            command({"name": 5, "args": {}}, held())


class TestAsAProgram:
    def program(
        self, given: Any, *, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the interpreter of the tests, and the program itself
            [sys.executable, "-E", "-s", str(PROGRAM)],
            input=given if isinstance(given, str) else json.dumps(given),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=clean_environment(**(environment or {})),
            timeout=60,
            check=False,
        )

    def test_what_it_is_given_on_standard_input_it_answers_on_standard_output(
        self, web: FakeWeb
    ) -> None:
        web.answer("POST", "/light", json_body=[])
        plan = plan_dict(
            step_http(
                "lamp",
                "POST",
                web.url("/light"),
                headers={"Authorization": "Bearer ${secret.HA_TOKEN}"},
                body={"brightness_pct": "${value.brightness}"},
            ),
            step_command("volume", "set_volume", level=30),
            say="The lamp is at ${value.brightness}.",
        )

        done = self.program(
            {
                "plan": plan,
                "params": {"brightness": 40},
                "device_params": {},
                "secrets": ["HA_TOKEN"],
                "budget_s": 20,
            },
            environment={"HA_TOKEN": "s3cr3t-token", "OTHER": "not-given"},
        )

        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == {
            "message": "The lamp is at 40.",
            "data": {"steps": {"lamp": {"status": 200}}},
            "commands": [{"name": "set_volume", "level": 30}],
        }
        assert web.seen[0].headers["authorization"] == "Bearer s3cr3t-token"
        assert web.seen[0].json() == {"brightness_pct": 40}

    def test_only_the_secrets_it_is_told_of_can_be_read(self, web: FakeWeb) -> None:
        plan = plan_dict(
            step_http("s", "GET", web.url("/x"), headers={"X": "${secret.OTHER}"}),
        )

        done = self.program(
            {"plan": plan, "secrets": ["HA_TOKEN"]},
            environment={"HA_TOKEN": "a", "OTHER": "not-given"},
        )

        assert done.returncode == 1
        assert "s: the secret OTHER is not there" in done.stderr
        assert web.seen == []

    def test_a_step_that_fails_is_exit_code_1_and_one_line_that_says_which(
        self, web: FakeWeb
    ) -> None:
        web.answer("GET", "/x", status=404)

        done = self.program({"plan": plan_dict(step_http("find", "GET", web.url("/x")))})

        assert done.returncode == 1
        assert done.stdout == ""
        assert done.stderr.strip() == "step find: the answer is 404"

    @pytest.mark.parametrize(
        "given",
        ["", "not json", "[]", "{}", '{"plan": 5}', '{"plan": {}, "budget_s": "soon"}', "null"],
    )
    def test_what_is_not_a_plan_to_run_is_exit_code_2(self, given: str) -> None:
        done = self.program(given)

        assert done.returncode == 2 or (given == '{"plan": 5}' and done.returncode == 1)
        assert done.stdout == ""

    def test_it_goes_through_the_proxy_the_environment_names_with_what_it_is_given_there(
        self, web: FakeWeb
    ) -> None:
        web.answer("GET", "/api/x", json_body={"ok": True})
        plan = plan_dict(step_http("s", "GET", "http://ha.example:8123/api/x"))

        done = self.program(
            {"plan": plan},
            environment={
                "HTTP_PROXY": f"http://skill:the-grant@127.0.0.1:{web.port}",
                "http_proxy": f"http://skill:the-grant@127.0.0.1:{web.port}",
            },
        )

        assert done.returncode == 0, done.stderr
        [seen] = web.seen
        assert seen.target == "http://ha.example:8123/api/x"
        assert seen.headers["proxy-authorization"].startswith("Basic ")

    def test_a_program_that_is_run_where_python_ignores_the_environment_still_works(
        self, web: FakeWeb
    ) -> None:
        web.answer("GET", "/x", text="ok")

        done = self.program(
            {"plan": plan_dict(step_http("s", "GET", web.url("/x")))},
            environment={"PYTHONPATH": "/nowhere", "PYTHONSTARTUP": "/nowhere"},
        )

        assert done.returncode == 0, done.stderr


class TestAResultOfAStep:
    def test_it_holds_what_the_answer_said(self) -> None:
        got = StepResult(200, "text", {"a": 1}, True)

        assert (got.status, got.text, got.document, got.has_document) == (
            200,
            "text",
            {"a": 1},
            True,
        )
