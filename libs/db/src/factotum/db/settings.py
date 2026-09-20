"""Where the database is, read from the environment.

The variable names are the ones ``docker compose`` reads to create the database
(``FACTOTUM_POSTGRES_USER``, ``..._PASSWORD``, ``..._DB``), so one ``.env`` serves both.
"""

from pydantic import Field, SecretStr

from factotum.common.config import ServiceSettings
from factotum.contracts.errors import FactotumError


class DatabaseNotConfiguredError(FactotumError):
    code = "database_not_configured"


class DatabaseSettings(ServiceSettings):
    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = "factotum"
    postgres_password: SecretStr | None = None
    """There is deliberately no default: a service that starts with a guessable password is worse
    than one that does not start."""

    postgres_db: str = "factotum"
    postgres_pool_max: int = Field(default=10, ge=1, le=200)

    def password(self) -> str:
        if self.postgres_password is None or not self.postgres_password.get_secret_value():
            raise DatabaseNotConfiguredError(
                "the database password is not set: set FACTOTUM_POSTGRES_PASSWORD"
            )
        return self.postgres_password.get_secret_value()
