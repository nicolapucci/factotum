"""Hashing and checking a password with ``scrypt``, from the standard library: a dependency buys
nothing here that is not already there.
"""

import hashlib
import hmac
import secrets

_N = 2**14
_R = 8
_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_SCHEME = "scrypt"


def hash_password(password: str) -> str:
    """A password, salted and hashed: what is kept, never the password itself."""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_KEY_BYTES
    )
    return f"{_SCHEME}${_N}${_R}${_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """Whether ``password`` is the one ``hashed`` was made from.

    Never raises: a hash of another shape or scheme is simply not a match, in constant time once
    the two are the same length (what the scheme and its cost were is not a secret).
    """
    try:
        scheme, n, r, p, salt_hex, derived_hex = hashed.split("$")
        if scheme != _SCHEME:
            return False
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(derived_hex)
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)
