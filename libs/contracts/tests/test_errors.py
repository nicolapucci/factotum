import pytest

from factotum.contracts.errors import (
    AuthError,
    CapabilityNotSupportedError,
    ChatNotConfiguredError,
    ContentFilteredError,
    ContextTooLongError,
    FactotumError,
    InvalidRequestError,
    PolicyViolationError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitedError,
    UrlNotAllowedError,
    error_class_for,
)


def all_errors[E: FactotumError](root: type[E]) -> list[type[E]]:
    found = [root]
    for subclass in root.__subclasses__():
        found.extend(all_errors(subclass))
    return found


def test_every_error_has_a_distinct_stable_code() -> None:
    codes = [error.code for error in all_errors(FactotumError)]

    assert len(codes) == len(set(codes))
    assert {
        AuthError,
        PolicyViolationError,
        UrlNotAllowedError,
        CapabilityNotSupportedError,
    } <= set(all_errors(FactotumError))


def test_every_error_but_the_base_ends_in_error() -> None:
    assert all(error.__name__.endswith("Error") for error in all_errors(FactotumError))


def test_only_provider_errors_that_may_pass_are_retryable() -> None:
    retryable = {error for error in all_errors(ProviderError) if error.retryable}

    assert retryable == {RateLimitedError, ProviderUnavailableError, ProviderTimeoutError}


def test_a_rate_limit_error_carries_how_long_to_wait() -> None:
    error = RateLimitedError("slow down", retry_after_s=2.5, details={"model": "x"})

    assert error.retry_after_s == 2.5
    assert error.details == {"model": "x"}
    assert RateLimitedError("slow down").retry_after_s is None


def test_provider_errors_are_caught_as_provider_errors() -> None:
    for error in (
        ProviderAuthError,
        ContextTooLongError,
        InvalidRequestError,
        ContentFilteredError,
    ):
        with pytest.raises(ProviderError):
            raise error("nope")


def test_an_error_keeps_its_message_and_details() -> None:
    error = PolicyViolationError("no way", details={"reason": "device_policy"})

    assert str(error) == "no way"
    assert error.message == "no way"
    assert error.details == {"reason": "device_policy"}


def test_details_are_copied_so_callers_cannot_alias_them() -> None:
    details = {"a": 1}

    error = FactotumError("boom", details=details)
    details["a"] = 2

    assert error.details == {"a": 1}


def test_details_default_to_empty() -> None:
    assert FactotumError("boom").details == {}


def test_specific_errors_are_caught_as_the_base_class() -> None:
    with pytest.raises(FactotumError) as caught:
        raise AuthError("who are you")

    assert caught.value.code == "auth_error"


def test_the_error_that_a_code_stands_for_can_be_found_again() -> None:
    assert error_class_for("chat_not_configured") is ChatNotConfiguredError
    assert error_class_for("rate_limited") is RateLimitedError
    assert error_class_for("factotum_error") is FactotumError


def test_a_code_nobody_knows_stands_for_nothing() -> None:
    assert error_class_for("no_such_error") is None
    assert error_class_for("") is None


def test_every_error_is_found_by_its_own_code() -> None:
    for error in all_errors(FactotumError):
        assert error_class_for(error.code) is error
