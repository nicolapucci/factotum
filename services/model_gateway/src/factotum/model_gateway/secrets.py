"""Turning a reference to a secret into the secret, at the last moment and in memory only."""

import os
from collections.abc import Mapping
from typing import Protocol

from pydantic import SecretStr

from factotum.contracts.errors import SecretNotFoundError


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str | None) -> SecretStr | None:
        """The secret behind a reference, or None when the binding has none (a local server)."""
        ...


class EnvSecretResolver:
    """Resolves ``env:NAME`` from the environment. Later resolvers will read an encrypted store."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def resolve(self, secret_ref: str | None) -> SecretStr | None:
        if secret_ref is None:
            return None
        scheme, _, name = secret_ref.partition(":")
        if scheme != "env":
            raise SecretNotFoundError(
                f"unsupported secret reference scheme {scheme!r}",
                details={"secret_ref": secret_ref},
            )
        value = self._environ.get(name)
        if not value:
            raise SecretNotFoundError(
                f"the environment variable {name} is not set", details={"secret_ref": secret_ref}
            )
        return SecretStr(value)
