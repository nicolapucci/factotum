from factotum.common.credentials import (
    TOKEN_BYTES,
    hash_credential,
    new_credential,
    verify_credential,
)


def test_a_fresh_credential_verifies_against_its_own_hash() -> None:
    token = new_credential()

    assert verify_credential(token, hash_credential(token))


def test_the_wrong_token_does_not_verify() -> None:
    hashed = hash_credential(new_credential())

    assert not verify_credential("something else", hashed)


def test_two_fresh_credentials_are_not_the_same() -> None:
    assert new_credential() != new_credential()


def test_a_credential_is_long_enough_to_not_be_guessed() -> None:
    # base64url of TOKEN_BYTES random bytes: at least that many characters.
    assert len(new_credential()) >= TOKEN_BYTES


def test_hashing_is_deterministic_unlike_a_salted_password_hash() -> None:
    token = new_credential()

    assert hash_credential(token) == hash_credential(token)
