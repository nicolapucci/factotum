"""The credential that says what a run may reach: made by the runner, checked by the proxy."""

import base64
import hashlib
import hmac
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.common.egress import MAX_HOSTS, MAX_TOKEN_CHARS, MIN_KEY_BYTES, Grant, issue, verify
from factotum.contracts.errors import EgressGrantError

KEY = b"k" * 32
NOW = 1_800_000_000

_LABEL = st.from_regex(r"[a-z0-9]([a-z0-9-]{0,20}[a-z0-9])?", fullmatch=True)
HOSTS = st.lists(
    st.builds(lambda parts: ".".join(parts), st.lists(_LABEL, min_size=1, max_size=4)),
    max_size=MAX_HOSTS,
)
RUNS = st.from_regex(r"[A-Za-z0-9._-]{1,64}", fullmatch=True)


def grant_for(**changes: object) -> str:
    fields: dict[str, object] = {
        "run": "run-1",
        "hosts": ["homeassistant.local", "*.example.org"],
        "expires_at": NOW + 60,
    }
    return issue(KEY, **{**fields, **changes})  # type: ignore[arg-type]


def re_signed(payload: dict[str, object], *, key: bytes = KEY) -> str:
    """A token as the runner would make it, but for what a test wants to say in it."""
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    signature = hmac.new(key, f"v1.{body}".encode(), hashlib.sha256).digest()
    return f"v1.{body}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


class TestWhatIsIssuedIsWhatIsRead:
    def test_a_grant_says_which_run_it_is_for_what_it_may_reach_and_until_when(self) -> None:
        grant = verify(KEY, grant_for(), now=NOW)

        assert grant == Grant(
            run="run-1", hosts=("homeassistant.local", "*.example.org"), expires_at=NOW + 60
        )

    @given(run=RUNS, hosts=HOSTS, expires_at=st.integers(min_value=NOW, max_value=NOW + 10**6))
    def test_whatever_is_issued_is_read_back(
        self, run: str, hosts: list[str], expires_at: int
    ) -> None:
        token = issue(KEY, run=run, hosts=hosts, expires_at=expires_at)

        grant = verify(KEY, token, now=NOW)

        assert grant.run == run
        assert grant.hosts == tuple(dict.fromkeys(host.lower() for host in hosts))
        assert grant.expires_at == expires_at

    def test_a_grant_with_no_hosts_is_a_run_that_may_reach_nothing(self) -> None:
        assert verify(KEY, grant_for(hosts=[]), now=NOW).hosts == ()

    def test_hosts_are_lower_case_and_listed_once(self) -> None:
        token = grant_for(hosts=["API.Example.org", "api.example.org"])

        assert verify(KEY, token, now=NOW).hosts == ("api.example.org",)

    def test_it_is_a_string_that_goes_in_a_header(self) -> None:
        token = grant_for()

        assert token.isascii()
        assert token.count(".") == 2
        assert all(c.isalnum() or c in "._-" for c in token)
        assert len(token) < MAX_TOKEN_CHARS


class TestWhatIsNotAGrant:
    def test_one_that_another_key_made_is_not_one(self) -> None:
        with pytest.raises(EgressGrantError):
            verify(b"another-key-of-some-length", grant_for(), now=NOW)

    @given(
        position=st.integers(min_value=0),
        replacement=st.characters(min_codepoint=33, max_codepoint=126),
    )
    def test_one_that_was_altered_anywhere_is_not_one(
        self, position: int, replacement: str
    ) -> None:
        token = grant_for()
        at = position % len(token)
        if token[at] == replacement:
            return
        altered = token[:at] + replacement + token[at + 1 :]

        with pytest.raises(EgressGrantError):
            verify(KEY, altered, now=NOW)

    def test_hosts_cannot_be_added_to_one_that_was_made(self) -> None:
        version, payload, signature = grant_for(hosts=["a.org"]).split(".")
        body = json.loads(base64.urlsafe_b64decode(payload + "=="))
        body["h"].append("evil.org")
        forged = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")

        with pytest.raises(EgressGrantError):
            verify(KEY, f"{version}.{forged}.{signature}", now=NOW)

    def test_it_is_good_until_it_runs_out_and_not_after(self) -> None:
        token = grant_for(expires_at=NOW + 60)

        assert verify(KEY, token, now=NOW + 60).run == "run-1"
        with pytest.raises(EgressGrantError):
            verify(KEY, token, now=NOW + 61)

    @pytest.mark.parametrize(
        "token",
        [
            "",
            ".",
            "..",
            "v1",
            "v1.a",
            "v1.a.b",
            "v2.a.b",
            "v1.a.b.c",
            "not a token",
            "v1.%%%.%%%",
            "é.é.é",
            "x" * (MAX_TOKEN_CHARS + 1),
        ],
    )
    def test_what_is_not_even_the_shape_of_one(self, token: str) -> None:
        with pytest.raises(EgressGrantError):
            verify(KEY, token, now=NOW)

    @pytest.mark.parametrize(
        "payload",
        [
            {"r": "run", "h": [], "x": NOW + 60, "extra": 1},
            {"r": "run", "h": []},
            {"r": 1, "h": [], "x": NOW + 60},
            {"r": "", "h": [], "x": NOW + 60},
            {"r": "a b", "h": [], "x": NOW + 60},
            {"r": "run", "h": "a.org", "x": NOW + 60},
            {"r": "run", "h": [1], "x": NOW + 60},
            {"r": "run", "h": ["http://a.org"], "x": NOW + 60},
            {"r": "run", "h": ["*"], "x": NOW + 60},
            {"r": "run", "h": ["a.org"] * (MAX_HOSTS + 1), "x": NOW + 60},
            {"r": "run", "h": [], "x": "soon"},
            {"r": "run", "h": [], "x": True},
            {"r": "run", "h": [], "x": 1.5},
            [],
            "text",
            None,
        ],
    )
    def test_what_was_signed_but_is_not_a_grant_is_not_one(self, payload: object) -> None:
        with pytest.raises(EgressGrantError):
            verify(KEY, re_signed(payload), now=NOW)  # type: ignore[arg-type]

    def test_a_grant_that_is_not_json_is_not_one(self) -> None:
        body = base64.urlsafe_b64encode(b"\xff\xfe not json").decode().rstrip("=")
        signature = hmac.new(KEY, f"v1.{body}".encode(), hashlib.sha256).digest()
        token = f"v1.{body}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"

        with pytest.raises(EgressGrantError):
            verify(KEY, token, now=NOW)

    def test_one_that_is_too_big_is_refused_before_anything_is_worked_out_of_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        big = re_signed({"r": "run", "h": [], "x": NOW + 60, "pad": "x" * MAX_TOKEN_CHARS})
        assert len(big) > MAX_TOKEN_CHARS

        def looked_at(*_: object) -> str:
            raise AssertionError("a token that is too big was looked at")

        monkeypatch.setattr("factotum.common.egress._sign", looked_at)

        with pytest.raises(EgressGrantError):
            verify(KEY, big, now=NOW)

    def test_the_error_never_says_which_part_was_wrong(self) -> None:
        messages = set()
        for token in ["", "v1.a.b", grant_for(expires_at=NOW - 1), grant_for()[:-3] + "AAA"]:
            with pytest.raises(EgressGrantError) as raised:
                verify(KEY, token, now=NOW)
            messages.add(raised.value.message)

        assert messages == {"that is not a grant of the runner"}


class TestWhatCannotBeIssued:
    @pytest.mark.parametrize("run", ["", "a b", "a/b", "x" * 65, "é"])
    def test_a_run_has_a_plain_name(self, run: str) -> None:
        with pytest.raises(ValueError, match="run is called"):
            issue(KEY, run=run, hosts=[], expires_at=NOW)

    @pytest.mark.parametrize("host", ["http://a.org", "a.org/x", "*", "a b", ""])
    def test_a_host_is_a_name_or_an_address(self, host: str) -> None:
        with pytest.raises(ValueError, match="grant lists"):
            issue(KEY, run="r", hosts=[host], expires_at=NOW)

    def test_a_few_hosts_and_not_more(self) -> None:
        issue(KEY, run="r", hosts=[f"h{i}.org" for i in range(MAX_HOSTS)], expires_at=NOW)
        with pytest.raises(ValueError, match="grant lists"):
            issue(KEY, run="r", hosts=[f"h{i}.org" for i in range(MAX_HOSTS + 1)], expires_at=NOW)

    def test_a_key_that_is_too_short_is_not_used_at_all(self) -> None:
        short = b"k" * (MIN_KEY_BYTES - 1)

        with pytest.raises(ValueError, match="at least"):
            issue(short, run="r", hosts=[], expires_at=NOW)
        with pytest.raises(ValueError, match="at least"):
            verify(short, grant_for(), now=NOW)
