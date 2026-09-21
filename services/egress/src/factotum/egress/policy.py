"""Where a run may go: which hosts, and which of the addresses they name.

A run may reach the hosts its grant lists and nothing else. Which host a request is for is what the
request says; what it is really going to reach is what that name is resolved to, and that is checked
too, for every address, before anything is connected:

* An address that is not a place a skill can go is refused whatever the grant says: this machine
  (the proxy's own, which is nothing of the skill's), link-local addresses (which is where the
  metadata service of a cloud is), multicast, unspecified and reserved ones.
* A private address (the LAN: a lamp, a hub, a router) is reached only by a host that the grant
  *names*. A name with a wildcard in front of it (``*.example.org``) stands for whatever a domain
  answers for, and a domain can answer with an address in the house.
* A public address is reached by any host the grant lists.

An address that is wrapped in another (an IPv4 address written as IPv6) is the address it wraps.
"""

import ipaddress
from collections.abc import Sequence
from enum import StrEnum

from factotum.common.urlguard import IpClass, classify_ip, unwrap_ip


class Match(StrEnum):
    EXACT = "exact"
    """The grant names the host."""

    WILDCARD = "wildcard"
    """The grant lists a domain that the host is in."""


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
    """How the grant lists a host, or nothing when it does not. A name that the grant gives whole
    is better than a domain it lists, and an address is only ever matched by itself."""
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


def refusal_of_address(address: str, match: Match) -> str | None:
    """Why an address may not be connected to for a host that was listed the way ``match`` says, or
    nothing when it may."""
    try:
        ip = unwrap_ip(address)
    except ValueError:
        return "it is not an address"
    if ip.is_loopback or ip.is_unspecified:
        return "it is this machine"
    kind = classify_ip(address)
    if kind is IpClass.FORBIDDEN:
        return "it is not a place a skill can go"
    if kind is IpClass.PRIVATE and match is not Match.EXACT:
        return "it is on a private network, and only a host that the grant names may go there"
    return None
