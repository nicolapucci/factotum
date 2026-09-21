"""Configuration of the egress proxy, read from the environment.

The proxy knows one secret, the key it shares with the runner (the runner signs what a run may
reach and the proxy checks it), and nothing else about the system: it does not know a model, a
skill, a device or a tenant.
"""

from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import NoDecode

from factotum.common.config import ServiceSettings

MIN_KEY_CHARS = 32


class EgressSettings(ServiceSettings):
    egress_host: str = "127.0.0.1"
    """The interface to listen on. Loopback unless told otherwise: a deployment puts it on the
    network of the sandboxes, and nowhere else."""

    egress_port: int = Field(default=3128, ge=1, le=65535)

    egress_key: SecretStr | None = None
    """The key that the runner signs grants with (at least 32 characters, and no other service
    gets it). Without one the proxy does not start: there would be nothing to check a grant by."""

    egress_connect_ports: Annotated[tuple[int, ...], NoDecode] = (443,)
    """The ports that a tunnel (a connection to a host that is not spoken to in the clear: https)
    may be made to. A request in the clear may go to any port of a host that was listed."""

    egress_max_connections: int = Field(default=64, ge=1, le=10_000)
    """How many runs are being served at once; the next is told to try again."""

    egress_header_timeout_s: float = Field(default=10.0, gt=0, le=120)
    """How long a request may take to say what it is."""

    egress_connect_timeout_s: float = Field(default=10.0, gt=0, le=120)
    """How long the host that was asked for may take to answer."""

    egress_idle_timeout_s: float = Field(default=30.0, gt=0, le=600)
    """How long a connection may say nothing at all before it is cut."""

    egress_max_duration_s: float = Field(default=130.0, gt=0, le=3600)
    """How long a connection may last: as long as the longest run, and not longer."""

    egress_max_mb: int = Field(default=20, ge=1, le=1024)
    """How much may go through a connection, in both directions."""

    @field_validator("egress_connect_ports", mode="before")
    @classmethod
    def _ports_are_listed_by_commas(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(int(part) for part in value.split(",") if part.strip())
        return value

    @field_validator("egress_connect_ports")
    @classmethod
    def _ports_are_ports(cls, ports: tuple[int, ...]) -> tuple[int, ...]:
        if not ports or any(not 1 <= port <= 65535 for port in ports):
            raise ValueError("at least one port, each between 1 and 65535")
        return ports

    @model_validator(mode="after")
    def _the_key_is_a_key(self) -> Self:
        if self.egress_key is not None and len(self.egress_key.get_secret_value()) < MIN_KEY_CHARS:
            raise ValueError(f"FACTOTUM_EGRESS_KEY is at least {MIN_KEY_CHARS} characters")
        return self

    @property
    def key(self) -> bytes:
        """The key as bytes. Raises ``ValueError`` when there is none."""
        if self.egress_key is None:
            raise ValueError("FACTOTUM_EGRESS_KEY is not set")
        return self.egress_key.get_secret_value().encode("utf-8")
