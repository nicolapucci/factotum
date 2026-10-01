import pytest

from factotum.common.tokens import MIN_SECRET_BYTES, _encode, _sign, decode_token, encode_token
from factotum.contracts.errors import AuthError
from factotum.contracts.testing import make_device_id, make_tenant_principal, make_user_principal

SECRET = b"x" * MIN_SECRET_BYTES
OTHER_SECRET = b"y" * MIN_SECRET_BYTES


def test_a_token_round_trips_to_the_same_principal() -> None:
    principal = make_user_principal(device_id=make_device_id())

    token = encode_token(principal, secret=SECRET, ttl_s=60)

    assert decode_token(token, secret=SECRET) == principal


def test_a_tenant_principal_round_trips_too() -> None:
    principal = make_tenant_principal()

    token = encode_token(principal, secret=SECRET, ttl_s=60)

    assert decode_token(token, secret=SECRET) == principal


def test_a_token_is_not_good_before_it_expires() -> None:
    principal = make_user_principal()
    token = encode_token(principal, secret=SECRET, ttl_s=60, now=1_000)

    assert decode_token(token, secret=SECRET, now=1_060) == principal
    with pytest.raises(AuthError, match="not valid or has expired"):
        decode_token(token, secret=SECRET, now=1_061)


def test_the_wrong_secret_is_refused() -> None:
    token = encode_token(make_user_principal(), secret=SECRET, ttl_s=60)

    with pytest.raises(AuthError):
        decode_token(token, secret=OTHER_SECRET)


@pytest.mark.parametrize(
    "broken",
    [
        "",
        "not-a-token",
        "v1.onlyonepart",
        "v1.a.b.c",
        "v2.a.b",
        "v1.not-base64!!!.sig",
    ],
)
def test_a_token_that_is_not_one_is_refused_the_same_way(broken: str) -> None:
    with pytest.raises(AuthError, match="not valid or has expired"):
        decode_token(broken, secret=SECRET)


def test_a_payload_that_does_not_decode_to_json_is_refused() -> None:
    garbled = _encode(b"not json")
    signed = f"v1.{garbled}"
    forged = f"{signed}.{_sign(SECRET, signed)}"

    with pytest.raises(AuthError, match="not valid or has expired"):
        decode_token(forged, secret=SECRET)


def test_a_secret_shorter_than_the_minimum_is_refused() -> None:
    short = b"x" * (MIN_SECRET_BYTES - 1)

    with pytest.raises(ValueError, match="at least"):
        encode_token(make_user_principal(), secret=short, ttl_s=60)
    with pytest.raises(ValueError, match="at least"):
        decode_token("v1.a.b", secret=short)


def test_a_tampered_signature_is_refused() -> None:
    token = encode_token(make_user_principal(), secret=SECRET, ttl_s=60)
    version, payload, signature = token.split(".")
    tampered = f"{version}.{payload}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}"

    with pytest.raises(AuthError):
        decode_token(tampered, secret=SECRET)


def test_a_tampered_payload_is_refused() -> None:
    token = encode_token(make_user_principal(), secret=SECRET, ttl_s=60)
    version, payload, signature = token.split(".")
    tampered = f"{version}.{payload}x.{signature}"

    with pytest.raises(AuthError):
        decode_token(tampered, secret=SECRET)
