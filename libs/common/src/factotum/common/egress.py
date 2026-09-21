"""What a skill may reach, as a credential that only the runner can make.

The sandbox of a skill sits on a network whose only exit is the egress proxy. Which hosts the proxy
lets a given run reach is not something the proxy keeps (that would be a state to hold in step with
the runner, for every run that is going on): it is written in the credential the run presents, and
signed by the runner with a key that it shares with the proxy alone. The proxy checks the signature
and the time, and then the host of every request against what the credential lists.

The credential is in the environment of the skill, so a skill can read it. That does no harm: it
says what that skill was let reach, it cannot be changed without the key, and it stops being good
when the run should have ended.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from factotum.contracts.errors import EgressGrantError
from factotum.contracts.skills import HOST_PATTERN

VERSION = "v1"
USER = "skill"
"""The user name a run presents to the proxy (the password is the grant)."""

MIN_KEY_BYTES = 16
MAX_TOKEN_CHARS = 7000
"""What fits in a header with room to spare: twenty hosts of the longest name make less than this
once they are encoded."""

MAX_HOSTS = 20
_RUN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_NOT_A_GRANT = "that is not a grant of the runner"


@dataclass(frozen=True, slots=True)
class Grant:
    run: str
    """Which run it was made for, so that what is done with it can be told in a log."""

    hosts: tuple[str, ...]
    """What it may reach: a name, a name with ``*.`` before it, or an address."""

    expires_at: int
    """Until when it is good, in seconds since the epoch."""


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(key: bytes, signed: str) -> str:
    return _encode(hmac.new(key, signed.encode("ascii"), hashlib.sha256).digest())


def _checked_key(key: bytes) -> bytes:
    if len(key) < MIN_KEY_BYTES:
        raise ValueError(f"the key of the egress is at least {MIN_KEY_BYTES} bytes")
    return key


def issue(key: bytes, *, run: str, hosts: Sequence[str], expires_at: int) -> str:
    """The grant for a run, to be given to it as the password of its proxy."""
    _checked_key(key)
    if not _RUN.fullmatch(run):
        raise ValueError(
            "a run is called by up to 64 letters, digits, dots, dashes and underscores"
        )
    listed = tuple(dict.fromkeys(host.lower() for host in hosts))
    if len(listed) > MAX_HOSTS or not all(HOST_PATTERN.fullmatch(host) for host in listed):
        raise ValueError("a grant lists a few hosts, and each is a name or an address")
    payload = _encode(
        json.dumps(
            {"r": run, "h": list(listed), "x": expires_at}, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )
    signed = f"{VERSION}.{payload}"
    token = f"{signed}.{_sign(key, signed)}"
    if len(token) > MAX_TOKEN_CHARS:
        raise ValueError("a grant is not a list of hosts as long as this")
    return token


def verify(key: bytes, token: str, *, now: int) -> Grant:
    """What the grant says, when it is one that the runner made and that has not run out. Raises
    ``EgressGrantError`` otherwise, and never says which of those it was."""
    _checked_key(key)
    fail = EgressGrantError(_NOT_A_GRANT)
    if len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
        raise fail
    version, payload, signature = token.split(".")
    if version != VERSION:
        raise fail
    expected = _sign(key, f"{version}.{payload}")
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii", "replace")):
        raise fail
    try:
        body: Any = json.loads(_decode(payload))
    except (ValueError, binascii.Error):
        raise fail from None
    grant = _read(body)
    if grant is None or now > grant.expires_at:
        raise fail
    return grant


def _read(body: Any) -> Grant | None:
    """The grant in a body that was signed, or nothing when it is not the shape of one (the runner
    of another version, or a key that was used for something else)."""
    if not isinstance(body, dict) or set(body) != {"r", "h", "x"}:
        return None
    run, hosts, expires_at = body["r"], body["h"], body["x"]
    if not isinstance(run, str) or not _RUN.fullmatch(run):
        return None
    if not isinstance(hosts, list) or len(hosts) > MAX_HOSTS:
        return None
    if not all(isinstance(host, str) and HOST_PATTERN.fullmatch(host) for host in hosts):
        return None
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        return None
    return Grant(run=run, hosts=tuple(hosts), expires_at=expires_at)
