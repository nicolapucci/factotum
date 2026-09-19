"""Whether an endpoint is under the user's control or somebody else's."""

import ipaddress
from urllib.parse import urlsplit

from factotum.contracts.models import Locality

_LOCAL_NAMES = frozenset({"localhost", "host.docker.internal", "gateway.docker.internal"})
_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa")


def infer_locality(base_url: str) -> Locality:
    """``LOCAL`` when the address is clearly this machine or the private network, else ``REMOTE``.

    It is only a default for a user who did not declare the locality. It errs on the side of
    ``REMOTE``: calling a local server remote costs some convenience (documents marked local-only
    cannot be sent to it), calling a remote server local would leak them.
    """
    host = urlsplit(base_url).hostname
    if not host:
        return Locality.REMOTE
    host = host.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return Locality.LOCAL if address.is_private or address.is_loopback else Locality.REMOTE
    if host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
        return Locality.LOCAL
    if "." not in host:
        # A bare name: a Docker or Kubernetes service, or a LAN host name.
        return Locality.LOCAL
    return Locality.REMOTE
