import pytest
from pydantic import ValidationError

from factotum.contracts.testing import make_user_principal
from factotum.contracts.tokens import TokenPayload


def test_a_token_payload_round_trips_through_json() -> None:
    payload = TokenPayload(principal=make_user_principal(), issued_at=100, expires_at=200)

    assert TokenPayload.model_validate_json(payload.model_dump_json()) == payload


def test_it_is_immutable_and_rejects_unknown_fields() -> None:
    payload = TokenPayload(principal=make_user_principal(), issued_at=100, expires_at=200)

    with pytest.raises(ValidationError):
        payload.expires_at = 300  # type: ignore[misc]
    with pytest.raises(ValidationError):
        TokenPayload.model_validate({**payload.model_dump(mode="json"), "not_a_field": True})
