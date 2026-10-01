"""Signing and checking a ``TokenPayload``, so a service can tell a ``Principal`` from a token
without asking the hub for every request it serves or every one it makes on somebody's behalf.

The shape mirrors the grant of ``factotum.common.egress``: a version, a payload and an HMAC-SHA256
signature, all base64url, joined by dots. The secret is shared by every service that mints or checks
a token: the hub mints one at login and at pairing, and a service that calls another re-mints one of
its own for the same principal, so the chain needs no call back to the hub. Holding the secret is
already what being part of the deployment means, the same trust the development headers assumed with
no check at all; a deployment that wants only the hub to mint needs asymmetric signatures instead,
which is not done here (see the ADR).
"""

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any

from factotum.contracts.errors import AuthError
from factotum.contracts.identity import Principal
from factotum.contracts.tokens import TokenPayload

VERSION = "v1"
MIN_SECRET_BYTES = 16
MAX_TOKEN_CHARS = 8192

_INVALID = "the token is not valid or has expired"


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: bytes, signed: str) -> str:
    return _encode(hmac.new(secret, signed.encode("ascii"), hashlib.sha256).digest())


def _checked_secret(secret: bytes) -> bytes:
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError(f"a token secret is at least {MIN_SECRET_BYTES} bytes")
    return secret


def encode_token(principal: Principal, *, secret: bytes, ttl_s: int, now: int | None = None) -> str:
    """A signed token that proves ``principal``, good for ``ttl_s`` seconds from ``now``."""
    _checked_secret(secret)
    issued = now if now is not None else int(time.time())
    payload = TokenPayload(principal=principal, issued_at=issued, expires_at=issued + ttl_s)
    encoded = _encode(payload.model_dump_json().encode("utf-8"))
    signed = f"{VERSION}.{encoded}"
    token = f"{signed}.{_sign(secret, signed)}"
    if len(token) > MAX_TOKEN_CHARS:
        raise ValueError("a token does not carry this much")
    return token


def decode_token(token: str, *, secret: bytes, now: int | None = None) -> Principal:
    """The ``Principal`` a token proves, when it is one this secret signed and it has not expired.

    Raises ``AuthError`` otherwise, and never says which of those it was.
    """
    _checked_secret(secret)
    fail = AuthError(_INVALID)
    if len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
        raise fail
    version, encoded, signature = token.split(".")
    if version != VERSION:
        raise fail
    expected = _sign(secret, f"{version}.{encoded}")
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii", "replace")):
        raise fail
    try:
        body: Any = json.loads(_decode(encoded))
        payload = TokenPayload.model_validate(body)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise fail from None
    checked_now = now if now is not None else int(time.time())
    if checked_now > payload.expires_at:
        raise fail
    return payload.principal
