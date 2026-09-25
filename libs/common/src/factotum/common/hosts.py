"""Which host a list of hosts names, the way a grant lists them.

A skill is granted hosts (``api.example.org``, ``*.example.org``, ``192.168.1.20``), and the same
question is asked in two places: the proxy that a script goes out through asks it of every request,
and the checks on the plan a model wrote ask it of every URL before anything is sent. It is one
function so that the two cannot disagree about what a host is.
"""

import ipaddress
from collections.abc import Sequence
from enum import StrEnum


class Match(StrEnum):
    EXACT = "exact"
    """The list names the host."""

    WILDCARD = "wildcard"
    """The list has a domain that the host is in."""


def normalize_host(host: str) -> str:
    """A host as it is compared: in lower case, and without the dot that ends a name."""
    return host.strip().lower().rstrip(".")


def is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def match_host(listed: Sequence[str], host: str) -> Match | None:
    """How the list has a host, or nothing when it does not. A name that the list gives whole is
    better than a domain it has, and an address is only ever matched by itself."""
    wanted = normalize_host(host)
    if not wanted:
        return None
    if wanted in listed:
        return Match.EXACT
    if is_address(wanted):
        return None
    for entry in listed:
        if entry.startswith("*.") and wanted.endswith(entry[1:]) and len(wanted) > len(entry) - 1:
            return Match.WILDCARD
    return None
