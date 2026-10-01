"""Configuration shared by every service, read from the environment (twelve-factor).

Every field is read from an environment variable named ``FACTOTUM_<FIELD>`` and, in development,
from a ``.env`` file. Each service subclasses ``ServiceSettings`` and adds its own fields. Every
field must be documented in ``.env.example``: a test enforces it, so the example cannot drift.
"""

from enum import StrEnum
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from factotum.contracts.identity import TenantMode


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FACTOTUM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.PRODUCTION
    """Unset means production: forgetting to configure it switches developer conveniences off
    instead of on."""

    tenant_mode: TenantMode = TenantMode.SINGLE_TENANT
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    hub_token_secret: SecretStr | None = None
    """What every service uses to mint and check a token that proves a ``Principal``
    (``factotum.common.hubauth``). Unset, a service falls back to the development authenticator,
    which does not exist outside development and test: production needs this set."""

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def hub_secret_bytes(self) -> bytes | None:
        if self.hub_token_secret is None:
            return None
        return self.hub_token_secret.get_secret_value().encode("utf-8")
