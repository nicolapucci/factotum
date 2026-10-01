"""What a signed token says: a ``Principal``, and for how long.

The token itself (a compact signed string) is not a contract, because nothing parses it as JSON
directly: ``factotum.common.tokens`` signs and checks it, and hands back (or takes) this shape.
"""

from factotum.contracts.base import Contract
from factotum.contracts.identity import Principal


class TokenPayload(Contract):
    """A ``Principal``, signed, good between two points in time."""

    principal: Principal
    issued_at: int
    expires_at: int
