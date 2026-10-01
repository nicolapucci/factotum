"""Hashing and checking a device's credential: a fast digest, not a slow password hash.

A device's token is random (``new_credential``), already far too long and unpredictable to guess or
to brute force from its hash: the deliberate slowness of a password hash would only cost every
device every time it connects, for no benefit a random token needs. A plain SHA-256 digest is
enough, checked in constant time, and it needs no secret: any service that was told what the hub
hashed with can check one itself, which is what lets a registry built from the hub's device list
verify a device locally, the same way ``factotum.common.tokens`` lets a token be checked locally.
"""

import hashlib
import hmac
import secrets

TOKEN_BYTES = 32


def new_credential() -> str:
    """A fresh, random credential for a device to present."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_credential(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_credential(token: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_credential(token), hashed)
