"""The secrets that a skill is given, and whose they are.

A skill declares the secrets it needs by name (``HA_TOKEN``), an administrator grants them when the
version is enabled, and what it is given at the run is the *value* of each, which is somebody's:
the tenant's (the token of the home's Home Assistant) or a person's own. A skill is only ever given
the secrets of its own owner. A skill that a person wrote for themselves that asked for ``HA_TOKEN``
would otherwise be a way to have the tenant's token sent to a host of the person's choosing; with
the owner in the way, it is refused a token that is not theirs, however it is granted.

Where the values are kept is the hub's business (slice 7). Until then they are in a file, for
development, which refuses to exist outside development and tests like the other development
conveniences, and a service that has none gives a skill nothing: one that needs a secret then fails,
and says which, never what it was.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

import yaml

from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.errors import AuthError, SecretNotFoundError
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import TenantId, UserId
from factotum.contracts.skills import is_reserved_env_name


class SecretResolver(Protocol):
    async def resolve(
        self, tenant_id: TenantId, owner: Owner, names: Sequence[str]
    ) -> dict[str, str]:
        """The values of the secrets ``names`` that belong to ``owner``. Raises
        ``SecretNotFoundError``, naming the secret and never saying a value, for one that is not
        set for them."""
        ...


class NoSecrets:
    """A resolver of a deployment that keeps none: a skill that needs one cannot run."""

    async def resolve(
        self, tenant_id: TenantId, owner: Owner, names: Sequence[str]
    ) -> dict[str, str]:
        if names:
            raise SecretNotFoundError(f"the secret {names[0]} is not set")
        return {}


def parse_secrets(
    text: str, *, tenant_id: TenantId, user_id: UserId, source: str = "secrets"
) -> dict[Owner, dict[str, str]]:
    """The secrets that a YAML document holds, by whose they are: ``tenant`` (the tenant's, which
    are for the skills of the tenant) and ``user`` (the development user's own). Raises
    ``ValueError``, saying where and never what, when it is not one."""
    document = yaml.safe_load(text)
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ValueError(f"{source}: expected a mapping with 'tenant' and 'user'")
    unknown = set(document) - {"tenant", "user"}
    if unknown:
        raise ValueError(f"{source}: unknown top-level keys: {sorted(unknown)}")
    owners = {
        "tenant": Owner.of_subject(Subject.tenant(tenant_id)),
        "user": Owner.of_subject(Subject.user(user_id)),
    }
    held: dict[Owner, dict[str, str]] = {}
    for section, owner in owners.items():
        values = document.get(section) or {}
        if not isinstance(values, dict):
            raise ValueError(f"{source}: {section} is a mapping of names to values")
        checked: dict[str, str] = {}
        for name, value in values.items():
            if not isinstance(name, str) or is_reserved_env_name(name):
                raise ValueError(f"{source}: {section}: {name!r} cannot be the name of a secret")
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"{source}: {section}: the value of {name} is a text (quote it), not empty"
                )
            checked[name] = value
        held[owner] = checked
    return held


class FileSecrets:
    """The secrets of a home, from a file (development only)."""

    def __init__(
        self,
        settings: ServiceSettings,
        held: Mapping[Owner, Mapping[str, str]],
        *,
        tenant_id: TenantId,
    ) -> None:
        if settings.environment not in {Environment.DEVELOPMENT, Environment.TEST}:
            raise AuthError(
                "the file of secrets is disabled outside the development and test "
                f"environments (environment is {settings.environment.value!r}): secrets are kept "
                "by the hub"
            )
        self._held = {owner: dict(values) for owner, values in held.items()}
        self._tenant_id = tenant_id

    async def resolve(
        self, tenant_id: TenantId, owner: Owner, names: Sequence[str]
    ) -> dict[str, str]:
        mine = self._held.get(owner, {}) if tenant_id == self._tenant_id else {}
        values: dict[str, str] = {}
        for name in names:
            if name not in mine:
                raise SecretNotFoundError(f"the secret {name} is not set")
            values[name] = mine[name]
        return values


def load_secrets(
    path: Path, settings: ServiceSettings, *, tenant_id: TenantId, user_id: UserId
) -> FileSecrets:
    """The secrets of the file at ``path``. Raises ``ValueError`` (saying where) when the file is
    not one, and ``FileNotFoundError`` when it is not there."""
    held = parse_secrets(
        path.read_text(encoding="utf-8"), tenant_id=tenant_id, user_id=user_id, source=path.name
    )
    return FileSecrets(settings, held, tenant_id=tenant_id)
