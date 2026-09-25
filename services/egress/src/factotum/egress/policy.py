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

from factotum.common.hosts import Match
from factotum.common.urlguard import IpClass, classify_ip, unwrap_ip


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
