"""Base class shared by every contract."""

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """Immutable model that rejects unknown fields.

    Every value that crosses a service boundary derives from this class, so a typo in a payload
    fails loudly instead of being silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
