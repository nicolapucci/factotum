"""What a plan may do: every rule that stands between what a model wrote and a request being sent,
tested on its own, and as a property of the rules together."""

from collections.abc import Sequence
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.actions.instructions.checks import PlanRejected, check_plan, command_names, describe
from factotum.actions.instructions.testing import plan_dict, step_command, step_http
from factotum.contracts.effects import Effect
from factotum.contracts.instructions import InstructionPlan
from factotum.contracts.skills import ParamValue, SkillManifest, SkillPermissions

HOSTS = ("ha.example",)
DEVICE: dict[str, ParamValue] = {"entity_id": "light.living_room", "host": "ha.example"}
VALUES: dict[str, ParamValue] = {"brightness": 40, "state": "on", "place": "Rome"}


def manifest(effect: Effect = Effect.REVERSIBLE) -> SkillManifest:
    return SkillManifest.model_validate(
        {
            "name": "scene",
            "description": "Set a scene.",
            "effect": effect.value,
            "target": "external",
            "parameters": {
                "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
                "state": {"type": "string"},
                "place": {"type": "string"},
                "colour": {"type": "string"},
            },
            "device_parameters": {"entity_id": {"type": "string"}, "host": {"type": "string"}},
            "permissions": {"network": ["${device.host}"], "secrets": ["HA_TOKEN"]},
        }
    )


def check(
    *steps: dict[str, Any],
    say: str | None = None,
    effect: Effect = Effect.REVERSIBLE,
    hosts: Sequence[str] = HOSTS,
    secrets: Sequence[str] = ("HA_TOKEN",),
    values: dict[str, ParamValue] | None = None,
    device: dict[str, ParamValue] | None = None,
) -> None:
    check_plan(
        InstructionPlan.model_validate(plan_dict(*steps, say=say)),
        manifest=manifest(effect),
        granted=SkillPermissions(secrets=tuple(secrets)),
        hosts=hosts,
        values=VALUES if values is None else values,
        device_params=DEVICE if device is None else device,
    )


def rejected(*steps: dict[str, Any], match: str, **kwargs: Any) -> None:
    with pytest.raises(PlanRejected, match=match):
        check(*steps, **kwargs)


LAMP = step_http(
    "lamp",
    "POST",
    "http://${device.host}:8123/api/services/light/turn_on",
    headers={"Authorization": "Bearer ${secret.HA_TOKEN}"},
    body={"entity_id": "${device.entity_id}", "brightness_pct": "${value.brightness}"},
)


class TestWhatIsAccepted:
    def test_a_request_to_the_host_of_the_device_with_a_secret_in_a_header(self) -> None:
        check(LAMP, say="The lamp is at ${value.brightness} percent.")

    def test_a_chain_of_requests_that_uses_what_the_first_got(self) -> None:
        check(
            step_http("geo", "GET", "https://ha.example/v1/search?name=${value.place}"),
            step_http(
                "now",
                "GET",
                "https://ha.example/v1/forecast?lat=${step.geo.json.results.0.latitude}",
            ),
            say="It is ${step.now.json.current.temperature_2m|round} degrees in ${value.place}.",
            effect=Effect.READ_ONLY,
        )

    def test_a_command_for_a_device(self) -> None:
        check(step_command("louder", "set_volume", level=30), say="Done.")

    def test_a_command_with_what_the_person_asked_for_in_it(self) -> None:
        check(step_command("louder", "set_volume", level="${value.brightness}"))

    def test_requests_and_commands_in_one_plan(self) -> None:
        check(LAMP, step_command("tell", "speak", text="Lamp at ${value.brightness}"))

    def test_a_text_body(self) -> None:
        check(step_http("note", "POST", "https://ha.example/topic", body="${value.place} says hi"))

    def test_a_plan_that_says_nothing(self) -> None:
        check(LAMP)

    @pytest.mark.parametrize(
        "url",
        [
            "http://ha.example/x",
            "https://ha.example/x",
            "HTTP://HA.EXAMPLE/x",
            "http://ha.example./x",
            "http://ha.example:8123/x",
            "http://ha.example",
            "http://ha.example?x=1",
            "http://ha.example#top",
        ],
    )
    def test_the_way_of_writing_the_granted_host_does_not_matter(self, url: str) -> None:
        check(step_http("s", "GET", url))

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
    def test_a_skill_that_may_change_things_may_use_any_method(self, method: str) -> None:
        check(step_http("s", method, "http://ha.example/x"), effect=Effect.IRREVERSIBLE)

    def test_a_domain_that_was_granted_lets_in_what_is_in_it(self) -> None:
        check(step_http("s", "GET", "https://api.example.org/x"), hosts=("*.example.org",))

    def test_an_address_that_was_granted_is_reached(self) -> None:
        check(step_http("s", "GET", "http://192.168.1.20:8123/x"), hosts=("192.168.1.20",))

    def test_a_secret_in_the_query_of_an_address(self) -> None:
        check(step_http("s", "GET", "https://ha.example/x?key=${secret.HA_TOKEN}"))


class TestWhereARequestMayGo:
    def test_a_host_that_was_not_granted_is_refused_and_the_message_says_which_are(self) -> None:
        rejected(
            step_http("s", "GET", "https://evil.example/x"),
            match=(
                r"step s: the host evil\.example is not one the skill may reach "
                r"\(it may reach: ha\.example\)"
            ),
        )

    def test_a_skill_that_was_granted_no_network_reaches_nothing(self) -> None:
        rejected(
            step_http("s", "GET", "https://ha.example/x"), match="it may reach: none", hosts=()
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.org/x",
            "https://evilexample.org/x",
            "https://example.org.evil.example/x",
            "https://x.example.org.evil.example/x",
        ],
    )
    def test_a_domain_lets_in_only_what_is_in_it(self, url: str) -> None:
        rejected(
            step_http("s", "GET", url),
            match="not one the skill may reach",
            hosts=("*.example.org",),
        )

    def test_an_address_is_reached_only_by_itself(self) -> None:
        rejected(
            step_http("s", "GET", "http://192.168.1.21/x"),
            match="not one the skill may reach",
            hosts=("192.168.1.20",),
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://user:pass@ha.example/x",
            "http://ha.example@evil.example/x",
            "http://ha.example:8123@evil.example/x",
            "http://evil.example\\@ha.example/x",
            "http://@ha.example/x",
        ],
    )
    def test_an_address_with_a_user_name_or_a_password_is_refused(self, url: str) -> None:
        rejected(step_http("s", "GET", url), match="user name or password")

    @pytest.mark.parametrize(
        "url",
        [
            "ha.example/x",
            "//ha.example/x",
            "ftp://ha.example/x",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "gopher://ha.example/",
            "ws://ha.example/",
            "/x",
        ],
    )
    def test_only_a_full_http_or_https_address_is_one(self, url: str) -> None:
        rejected(step_http("s", "GET", url), match="full http or https address")

    @pytest.mark.parametrize("url", ["http://[::1]/x", "http://[fd00::5]:8123/x"])
    def test_an_ipv6_address_is_not_supported(self, url: str) -> None:
        rejected(step_http("s", "GET", url), match="name or an IPv4 address", hosts=("::1",))

    @pytest.mark.parametrize("url", ["http://ha.example:0/x", "http://ha.example:70000/x"])
    def test_a_port_is_a_port(self, url: str) -> None:
        rejected(step_http("s", "GET", url), match="is not a port")

    @pytest.mark.parametrize(
        "url", ["http://ha.example:abc/x", "http://ha.example:-1/x", "http://ha.example:8 1/x"]
    )
    def test_a_port_is_digits(self, url: str) -> None:
        rejected(step_http("s", "GET", url), match="name or an IPv4 address|is not a host")

    @pytest.mark.parametrize(
        "url",
        [
            "http://ha%2eexample/x",
            "http://ha example/x",
            "http://ha.exampleé/x",
            "http://-ha.example/x",
            "http:///x",
            "http://:8123/x",
            "http://ha..example/x",
        ],
    )
    def test_a_host_that_is_not_one_is_refused(self, url: str) -> None:
        rejected(step_http("s", "GET", url), match="is not a host|not one the skill may reach")

    def test_the_host_of_the_device_is_put_in_and_checked(self) -> None:
        rejected(
            step_http("s", "GET", "http://${device.host}/x"),
            match=r"host other\.example is not one the skill may reach",
            device={"host": "other.example"},
        )

    def test_a_device_without_that_value_gives_no_host(self) -> None:
        rejected(
            step_http("s", "GET", "http://${device.nothing}/x"),
            match="the device has no such value",
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://${value.place}/x",
            "http://${secret.HA_TOKEN}/x",
            "http://ha.example.${value.place}/x",
            "http://${step.geo.json.host}/x",
        ],
    )
    def test_only_a_value_of_the_device_can_be_in_the_host_of_an_address(self, url: str) -> None:
        rejected(
            step_http("geo", "GET", "http://ha.example/g"),
            step_http("s", "GET", url),
            match="the host of an address is fixed",
        )

    def test_what_is_put_in_the_path_or_the_query_may_be_anything_that_is_there(self) -> None:
        check(
            step_http("geo", "GET", "http://ha.example/g"),
            step_http(
                "s",
                "GET",
                "http://ha.example/${value.place}/x?a=${step.geo.status}&k=${secret.HA_TOKEN}",
            ),
        )


class TestWhatASkillThatOnlyReadsMayDo:
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_it_makes_no_request_that_changes_anything(self, method: str) -> None:
        rejected(
            step_http("s", method, "http://ha.example/x"),
            match=f"the skill only reads, so its requests are GET, and this one is {method}",
            effect=Effect.READ_ONLY,
        )

    def test_it_reads(self) -> None:
        check(step_http("s", "GET", "http://ha.example/x"), effect=Effect.READ_ONLY)

    def test_the_first_request_that_does_not_read_is_the_one_that_is_named(self) -> None:
        rejected(
            step_http("a", "GET", "http://ha.example/x"),
            step_http("b", "POST", "http://ha.example/x"),
            match="step b:",
            effect=Effect.READ_ONLY,
        )


class TestSecrets:
    def test_a_secret_that_was_not_granted_is_refused(self) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x", headers={"X": "${secret.OTHER}"}),
            match=r"\$\{secret\.OTHER\}: the skill was not granted that secret",
        )

    def test_a_skill_that_was_granted_none_has_none(self) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x", headers={"X": "${secret.HA_TOKEN}"}),
            match="not granted",
            secrets=(),
        )

    def test_a_secret_is_not_said(self) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x"),
            say="The token is ${secret.HA_TOKEN}",
            match="what to say: .*a secret is put in a request and nowhere else",
        )

    def test_a_secret_is_not_given_to_a_device(self) -> None:
        rejected(
            step_command("tell", "speak", text="${secret.HA_TOKEN}"),
            match="a secret is put in a request and nowhere else",
        )

    def test_a_secret_is_fine_in_the_body_of_a_request(self) -> None:
        check(step_http("s", "POST", "http://ha.example/x", body={"key": "${secret.HA_TOKEN}"}))
        check(step_http("s", "POST", "http://ha.example/x", body="key=${secret.HA_TOKEN}"))


class TestWhatIsPutIn:
    def test_a_value_that_was_asked_for_and_is_not_there_is_refused(self) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x?c=${value.colour}"),
            match=r"\$\{value\.colour\}: that value was not given",
        )

    def test_a_value_that_the_skill_does_not_have_is_refused(self) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x?c=${value.speed}"),
            match="that value is not one of its values",
        )

    def test_a_value_of_the_device_that_is_not_there_is_refused_and_the_message_says_which_are(
        self,
    ) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x?c=${device.colour}"),
            match=r"the device has no such value \(it has: entity_id, host\)",
        )

    def test_only_a_number_is_rounded(self) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x?c=${value.place|round}"),
            match="only a number is rounded",
        )
        check(step_http("s", "GET", "http://ha.example/x?c=${value.brightness|round}"))

    def test_a_step_that_is_not_there_yet_cannot_be_used(self) -> None:
        rejected(
            step_http("a", "GET", "http://ha.example/x?c=${step.b.status}"),
            step_http("b", "GET", "http://ha.example/y"),
            match=r"step a: .*that is not a request done before \(done: none yet\)",
        )

    def test_a_step_cannot_use_itself(self) -> None:
        rejected(
            step_http("a", "GET", "http://ha.example/x?c=${step.a.status}"),
            match="not a request done before",
        )

    def test_a_step_that_does_not_exist_cannot_be_used(self) -> None:
        rejected(
            step_http("a", "GET", "http://ha.example/x"),
            step_http("b", "GET", "http://ha.example/y?c=${step.nothing.text}"),
            match=r"not a request done before \(done: a\)",
        )

    def test_a_command_is_not_a_step_whose_answer_can_be_used(self) -> None:
        rejected(
            step_command("tell", "speak", text="Hello"),
            step_http("s", "GET", "http://ha.example/x?c=${step.tell.status}"),
            match="not a request done before",
        )

    def test_what_is_to_be_said_can_use_the_steps_that_were_done_and_no_others(self) -> None:
        check(step_http("a", "GET", "http://ha.example/x"), say="It says ${step.a.text}")
        rejected(
            step_http("a", "GET", "http://ha.example/x"),
            say="It says ${step.b.text}",
            match="what to say: .*not a request done before",
        )

    @pytest.mark.parametrize(
        "text", ["${value.brightness", "${nothing.x}", "${value.a.b}", "${}", "${value.x|floor}"]
    )
    def test_a_placeholder_that_is_not_one_is_refused_wherever_it_is(self, text: str) -> None:
        rejected(step_http("s", "GET", "http://ha.example/x", headers={"X": text}), match="step s")
        rejected(step_http("s", "GET", "http://ha.example/x"), say=text, match="what to say")
        rejected(step_http("s", "POST", "http://ha.example/x", body=text), match="step s")

    def test_the_name_of_a_field_is_not_a_place_for_a_placeholder(self) -> None:
        rejected(
            step_http("s", "POST", "http://ha.example/x", body={"${value.place}": 1}),
            match="the name of a field is not a place for a placeholder",
        )
        rejected(
            step_http("s", "POST", "http://ha.example/x", body={"a": [{"${value.place}": 1}]}),
            match="the name of a field",
        )


class TestHeadersAndBodies:
    @pytest.mark.parametrize(
        "name",
        [
            "Host",
            "host",
            "Content-Length",
            "Transfer-Encoding",
            "Connection",
            "Keep-Alive",
            "Upgrade",
            "TE",
            "Trailer",
            "Expect",
            "Proxy-Authorization",
            "Proxy-Foo",
            "proxy-connection",
        ],
    )
    def test_what_the_program_sets_itself_is_not_the_plans_to_set(self, name: str) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x", headers={name: "x"}),
            match="is not the plan's to set",
        )

    @pytest.mark.parametrize("name", ["X A", "X:A", "X\nA", "é", "X{a}", "X\tA", ""])
    def test_a_name_that_is_not_a_header_is_refused(self, name: str) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x", headers={name: "x"}),
            match="is not the plan's to set",
        )

    @pytest.mark.parametrize("value", ["a\r\nX-B: c", "a\nb", "a\x00b", "a\x7fb"])
    def test_a_value_with_a_control_character_is_refused(self, value: str) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x", headers={"X-A": value}),
            match="has a control character",
        )

    def test_an_ordinary_header_is_fine(self) -> None:
        check(
            step_http(
                "s",
                "GET",
                "http://ha.example/x",
                headers={
                    "Accept": "application/json",
                    "X-Api-Key": "${secret.HA_TOKEN}",
                    "X-Tab": "c\td",
                },
            )
        )

    def test_a_get_has_no_body(self) -> None:
        rejected(
            step_http("s", "GET", "http://ha.example/x", body={"a": 1}),
            match="a GET has no body",
            effect=Effect.READ_ONLY,
        )


class TestCommands:
    @pytest.mark.parametrize("name", ["reboot", "SET_VOLUME", "set-volume", "shell", "play"])
    def test_a_command_that_is_not_in_the_vocabulary_is_refused_and_the_message_lists_them(
        self, name: str
    ) -> None:
        rejected(
            step_command("c", name),
            match=f"is not a command \\(they are: {', '.join(command_names())}\\)",
        )

    def test_the_vocabulary_is_the_one_the_devices_understand(self) -> None:
        assert set(command_names()) == {
            "speak",
            "play_url",
            "pause",
            "resume",
            "stop",
            "set_volume",
            "show_card",
            "clear_display",
            "set_led",
        }

    def test_an_argument_that_the_command_does_not_have_is_refused(self) -> None:
        rejected(
            step_command("c", "set_volume", loudness=3),
            match=r"set_volume has no argument called loudness \(it has: level\)",
        )

    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("set_volume", {"level": 101}),
            ("set_volume", {"level": "loud"}),
            ("speak", {}),
            ("speak", {"text": ""}),
        ],
    )
    def test_what_the_command_does_not_take_is_refused_when_it_is_known_now(
        self, name: str, args: dict[str, Any]
    ) -> None:
        rejected(step_command("c", name, **args), match=f"{name} does not take that")

    def test_what_is_not_known_until_it_runs_is_left_to_be_checked_then(self) -> None:
        check(step_command("c", "set_volume", level="${value.brightness}"))

    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("speak", {"text": "Hello", "language": "it"}),
            ("stop", {}),
            ("pause", {}),
            ("set_led", {"pattern": "blink", "color": "#ff0000"}),
            ("play_url", {"url": "https://a.example/x.mp3", "kind": "audio"}),
        ],
    )
    def test_a_command_with_what_it_takes_is_fine(self, name: str, args: dict[str, Any]) -> None:
        check(step_command("c", name, **args))

    def test_the_message_says_what_is_wrong_and_not_what_was_given(self) -> None:
        with pytest.raises(PlanRejected) as raised:
            check(step_command("c", "set_volume", level="hunter2-not-a-level"))

        assert "hunter2" not in str(raised.value)


class TestWhatAPlanIs:
    def test_a_plan_with_no_steps_does_nothing_and_is_refused(self) -> None:
        with pytest.raises(PlanRejected, match="does nothing"):
            check_plan(
                InstructionPlan.model_validate({"reason": "cannot", "say": "Sorry."}),
                manifest=manifest(),
                granted=SkillPermissions(),
                hosts=HOSTS,
                values={},
                device_params={},
            )

    def test_the_first_thing_that_is_wrong_is_what_is_said(self) -> None:
        rejected(
            step_http("a", "GET", "http://evil.example/x"),
            step_http("b", "GET", "http://also-evil.example/x"),
            match="step a: ",
        )

    def test_the_reason_is_short_however_long_what_was_wrong_is(self) -> None:
        with pytest.raises(PlanRejected) as raised:
            check(step_http("s", "GET", "https://" + "x" * 500 + ".example/x"))

        assert len(str(raised.value)) <= 300

    def test_the_reason_is_one_line(self) -> None:
        with pytest.raises(PlanRejected) as raised:
            check(step_command("c", "set_volume\nfoo"))

        assert "\n" not in str(raised.value)


class TestWhatIsSaidOfAPlan:
    def test_the_method_and_the_host_of_each_request_and_the_name_of_each_command(self) -> None:
        plan = InstructionPlan.model_validate(
            plan_dict(LAMP, step_command("tell", "speak", text="Hi"))
        )

        assert describe(plan) == "POST ${device.host}:8123, speak"

    def test_a_plan_with_nothing_in_it(self) -> None:
        assert describe(InstructionPlan.model_validate({"reason": "x"})) == "nothing"

    def test_it_is_short(self) -> None:
        steps = [step_http(f"s{n}", "GET", f"https://{'h' * 40}{n}.example/x") for n in range(8)]

        assert len(describe(InstructionPlan.model_validate(plan_dict(*steps)))) <= 200


_LABEL = st.from_regex(r"[a-z][a-z0-9]{0,8}", fullmatch=True)
_HOST = st.builds(lambda a, b: f"{a}.{b}.example", _LABEL, _LABEL)


class TestAsAProperty:
    @given(
        hosts=st.lists(_HOST, min_size=1, max_size=4, unique=True),
        granted=st.lists(_HOST, max_size=3, unique=True),
    )
    def test_a_plan_passes_only_if_every_request_goes_to_a_host_that_was_granted(
        self, hosts: list[str], granted: list[str]
    ) -> None:
        steps = [step_http(f"s{n}", "GET", f"https://{host}/x") for n, host in enumerate(hosts)]

        if all(host in granted for host in hosts):
            check(*steps, hosts=granted)
        else:
            with pytest.raises(PlanRejected):
                check(*steps, hosts=granted)

    @given(host=_HOST, other=_HOST)
    def test_a_domain_lets_in_its_own_and_no_other(self, host: str, other: str) -> None:
        domain = "*." + host.split(".", 1)[1]
        inside = step_http("s", "GET", f"https://{host}/x")
        outside = step_http("s", "GET", f"https://{other}.evil/x")

        check(inside, hosts=(domain,))
        with pytest.raises(PlanRejected):
            check(outside, hosts=(domain,))

    @given(
        method=st.sampled_from(["POST", "PUT", "PATCH", "DELETE"]),
        effect=st.sampled_from(list(Effect)),
    )
    def test_only_a_skill_that_reads_is_held_to_get(self, method: str, effect: Effect) -> None:
        step = step_http("s", method, "http://ha.example/x")

        if effect is Effect.READ_ONLY:
            with pytest.raises(PlanRejected):
                check(step, effect=effect)
        else:
            check(step, effect=effect)
