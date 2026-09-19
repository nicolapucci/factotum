from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.common.redaction import (
    REDACTED,
    TRUNCATED,
    is_sensitive_key,
    redact,
    redact_text,
)


class TestSensitiveKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "Authorization",
            "Proxy-Authorization",
            "X-Api-Key",
            "x-goog-api-key",
            "apiKey",
            "api_key",
            "password",
            "db_password",
            "client_secret",
            "refresh_token",
            "accessToken",
            "Set-Cookie",
            "private_key",
            "credentials",
        ],
    )
    def test_secret_keys_are_recognized(self, key: str) -> None:
        assert is_sensitive_key(key)

    @pytest.mark.parametrize(
        "key",
        [
            "max_tokens",
            "prompt_tokens",
            "token_count",
            "secrets",
            "primary_key",
            "sort_key",
            "name",
            "model",
            "user_id",
        ],
    )
    def test_ordinary_keys_are_left_alone(self, key: str) -> None:
        assert not is_sensitive_key(key)


class TestRedactText:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Authorization: Bearer abcdef1234567890", f"Authorization: Bearer {REDACTED}"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", f"Authorization: Basic {REDACTED}"),
            (
                "GET https://api.example.org/v1?key=AbCd1234&alt=json",
                f"GET https://api.example.org/v1?key={REDACTED}&alt=json",
            ),
            (
                "https://api.example.org/x?a=1&api_key=SECRET123&b=2",
                f"https://api.example.org/x?a=1&api_key={REDACTED}&b=2",
            ),
            ("postgres://factotum:hunter2@db:5432/x", f"postgres://factotum:{REDACTED}@db:5432/x"),
            ("used key sk-ant-api03-abcdefghijklmnop1234", f"used key {REDACTED}"),
            ("AIzaSyA1234567890abcdefghijklmnopqrstuv", REDACTED),
            ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", REDACTED),
            ("xoxb-1234567890-abcdefghij", REDACTED),
            (
                "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
                f"jwt {REDACTED}",
            ),
        ],
    )
    def test_secrets_recognized_by_their_shape_are_masked(self, text: str, expected: str) -> None:
        assert redact_text(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "the tokens were counted",
            "sk-short",
            "Bearer",
            "https://api.example.org/v1?model=gpt&stream=true",
            "a perfectly ordinary sentence",
        ],
    )
    def test_ordinary_text_is_untouched(self, text: str) -> None:
        assert redact_text(text) == text

    def test_several_secrets_in_one_string_are_all_masked(self) -> None:
        text = "calling https://u:pw123456@h/x?key=abcdef with Bearer abcdef1234567890"

        result = redact_text(text)

        assert "pw123456" not in result
        assert "abcdef1234567890" not in result
        assert "key=abcdef" not in result


class TestRedact:
    def test_values_under_a_sensitive_key_are_masked_whatever_they_are(self) -> None:
        payload = {"api_key": "anything", "password": {"nested": "value"}, "model": "llama"}

        assert redact(payload) == {"api_key": REDACTED, "password": REDACTED, "model": "llama"}

    def test_nothing_to_hide_stays_none(self) -> None:
        assert redact({"token": None}) == {"token": None}

    def test_nested_structures_are_walked(self) -> None:
        payload = {
            "request": {
                "headers": {"Authorization": "Bearer abcdef1234567890", "Accept": "json"},
                "url": "https://x.example.org/?key=abc123",
            },
            "items": [{"secret": "s"}, "sk-abcdefghijklmnop1234", 7],
        }

        assert redact(payload) == {
            "request": {
                "headers": {"Authorization": REDACTED, "Accept": "json"},
                "url": f"https://x.example.org/?key={REDACTED}",
            },
            "items": [{"secret": REDACTED}, REDACTED, 7],
        }

    def test_the_input_is_never_modified(self) -> None:
        payload = {"password": "x", "nested": {"token": "y"}}

        redact(payload)

        assert payload == {"password": "x", "nested": {"token": "y"}}

    def test_container_types_are_preserved(self) -> None:
        result = redact({"a": (1, "sk-abcdefghijklmnop1234"), "b": frozenset({"x"})})

        assert result == {"a": (1, REDACTED), "b": frozenset({"x"})}

    def test_bytes_are_decoded_and_masked(self) -> None:
        assert redact(b"Bearer abcdef1234567890") == f"Bearer {REDACTED}"

    def test_a_structure_that_contains_itself_does_not_recurse_forever(self) -> None:
        loop: list[Any] = []
        loop.append(loop)

        assert TRUNCATED in str(redact(loop))

    def test_other_values_pass_through(self) -> None:
        assert redact(3.5) == 3.5
        assert redact(True) is True


json_like = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=40)),
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=3),
    ),
    max_leaves=12,
)


@given(json_like)
def test_redaction_is_idempotent(value: Any) -> None:
    once = redact(value)

    assert redact(once) == once


@given(st.text(max_size=200))
def test_redact_text_never_raises_and_is_idempotent(text: str) -> None:
    once = redact_text(text)

    assert redact_text(once) == once
