"""Configuration of the hub, read from the environment.

The database is the one of ``DatabaseSettings``. Unlike every other service the hub always needs its
own token secret: it is the one every token is minted from, so there is no development fallback for
it, in any environment.
"""

from pydantic import Field

from factotum.db.settings import DatabaseSettings


class HubSettings(DatabaseSettings):
    hub_host: str = "127.0.0.1"
    hub_port: int = Field(default=8087, ge=1, le=65535)

    hub_admin_username: str = "admin"
    """The administrator of the tenant created at the first start (single-tenant mode only)."""

    hub_admin_password: str = "admin"  # noqa: S105 - a default to change, not a secret
    """Its password at the first start. Whichever value this has, the account is created with
    ``must_change_password`` set, so it must be changed at the first login regardless."""

    hub_session_hours: int = Field(default=12, ge=1, le=24 * 30)
    """How long a token from logging in is good for."""
