import pytest

from factotum.contracts.errors import (
    AuthError,
    CapabilityNotSupportedError,
    FactotumError,
    PolicyViolationError,
    UrlNotAllowedError,
)


def test_every_error_has_a_distinct_stable_code() -> None:
    subclasses = FactotumError.__subclasses__()
    codes = [FactotumError.code, *(subclass.code for subclass in subclasses)]

    assert len(codes) == len(set(codes))
    assert {
        AuthError,
        PolicyViolationError,
        UrlNotAllowedError,
        CapabilityNotSupportedError,
    } <= set(subclasses)


def test_an_error_keeps_its_message_and_details() -> None:
    error = PolicyViolationError("no way", details={"reason": "local_only"})

    assert str(error) == "no way"
    assert error.message == "no way"
    assert error.details == {"reason": "local_only"}


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
