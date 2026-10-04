"""How the services trust each other, in the two ways they can.

**In development** every service reads whom a call is for from headers, the devices are the file of
the example home and the secrets are the example file of secrets: what ``docker compose`` runs
until a token secret is set, and what refuses to exist in production.

**With the hub** (``Auth.HUB``) a token secret is shared, every service checks the signed token it
is shown and mints a short one of its own to call the next, and everything that was a file is the
hub's: its devices and the credential each was given when it was paired, the skills bound to them,
the models bound to a role, and the secrets of the home. The devices of the example home are paired
in a hub of this process, by its administrator, the way the documentation says; what each service
fetches from it is what ``build_app`` of that service fetches.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

import httpx
from fastapi import FastAPI

from factotum.actions.directory import DeviceDirectory, FileDeviceDirectory, HubDeviceDirectory
from factotum.actions.secrets import FileSecrets, HubSecretResolver, SecretResolver, parse_secrets
from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import (
    DEV_TENANT_ID,
    DEV_USER_ID,
    SUBJECT_HEADER,
    DevAuthenticator,
    DevPrincipalDelegation,
    PrincipalDelegation,
)
from factotum.common.hubauth import HubAuthenticator, HubPrincipalDelegation
from factotum.contracts.devices import ExternalDeviceRecord, SystemDeviceRecord
from factotum.contracts.hub import (
    CreateBindingRequest,
    CreateExternalDeviceRequest,
    CreateModelBindingRequest,
    CreateSystemDeviceRequest,
    DeviceOwner,
    LoginRequest,
    LoginResult,
    PairedDevice,
    SetSecretRequest,
)
from factotum.contracts.identity import SINGLE_TENANT_ID, OwnerKind
from factotum.contracts.models import ModelBinding
from factotum.devices.home import Home
from factotum.edge.registry import DeviceRegistry, FileDeviceRegistry, HubDeviceRegistry
from factotum.hub.api import Operations as HubOperations
from factotum.hub.api import create_app as create_hub_app
from factotum.hub.service import HubService
from factotum.hub.store.memory import InMemoryStore as HubMemory
from factotum.model_client import Delegation, DevDelegation, HubDelegation
from factotum.model_gateway.bindings import (
    BindingRepository,
    HubBindingRepository,
    InMemoryBindingRepository,
)
from factotum.web import Authenticator

TENANT = DEV_TENANT_ID
USER = DEV_USER_ID
"""The tenant and the person of every service in development, so that what one of them is told about
whom a device acts for is what the next one understands. The hub of a single-tenant deployment makes
its tenant with the same id."""

TEST = ServiceSettings(_env_file=None, environment=Environment.TEST)

HA_TOKEN = "the-token-of-the-home"  # noqa: S105 - a stand-in, in the secrets the example file shows
SECRETS = f'tenant:\n  HA_TOKEN: "{HA_TOKEN}"\n'
"""What the home keeps for its skills, as ``config/secrets.example.yaml`` has it."""

HUB_SECRET = b"the-token-secret-of-the-end-to-end-hub"
FIRST_PASSWORD = "the-first-password"  # noqa: S105 - the administrator of a hub of this process
ADMIN_PASSWORD = "a-password-the-administrator-chose"  # noqa: S105

Reach = Callable[[FastAPI, str], Awaitable[httpx.AsyncClient]]
"""How an application is reached: through its own HTTP interface, in memory."""


class Auth(StrEnum):
    DEV = "dev"
    HUB = "hub"


@dataclass
class Wiring:
    authenticator: Callable[[], Authenticator]
    delegation: Delegation
    principals: PrincipalDelegation
    repository: BindingRepository
    registry: DeviceRegistry
    directory: DeviceDirectory
    secrets: SecretResolver
    admin: dict[str, str]
    """The headers of the administrator that the tests act as."""

    tokens: dict[str, str] = field(default_factory=dict)
    """The credential each device was given when it was paired, by hardware id: with the hub only,
    since the credentials of the example file are placeholders that the hub never issued."""

    secret: bytes | None = None
    """The secret that signs the tokens, with the hub."""

    hub: httpx.AsyncClient | None = None


def dev_wiring(home: Home, models: list[ModelBinding]) -> Wiring:
    return Wiring(
        authenticator=lambda: DevAuthenticator(TEST, tenant_id=TENANT, user_id=USER),
        delegation=DevDelegation(TEST),
        principals=DevPrincipalDelegation(TEST),
        repository=InMemoryBindingRepository(models),
        registry=FileDeviceRegistry(TEST, list(home.system)),
        directory=FileDeviceDirectory(TEST, home),
        secrets=FileSecrets(
            TEST, parse_secrets(SECRETS, tenant_id=TENANT, user_id=USER), tenant_id=TENANT
        ),
        admin={SUBJECT_HEADER: "user"},
    )


async def hub_wiring(home: Home, models: list[ModelBinding], reach: Reach) -> Wiring:
    """A hub with the home paired in it by its administrator, and the services reading from it."""
    assert SINGLE_TENANT_ID == TENANT
    secret = HUB_SECRET
    service = HubService(HubMemory(), secret=secret, session_hours=1)
    await service.bootstrap(tenant_id=TENANT, username="admin", password=FIRST_PASSWORD)
    hub = await reach(
        create_hub_app(HubAuthenticator(secret), operations=HubOperations(service=service)), "hub"
    )
    admin = await _administrator(hub)
    principals = HubPrincipalDelegation(secret)

    tokens = await _pair(hub, admin, home, models)

    repository = HubBindingRepository(hub, principals, TENANT)
    registry = HubDeviceRegistry(hub, principals, TENANT)
    directory = HubDeviceDirectory(hub, principals, TENANT)
    secrets = HubSecretResolver(hub, principals, TENANT)
    for reloadable in (repository, registry, directory, secrets):
        await reloadable.reload()
    return Wiring(
        authenticator=lambda: HubAuthenticator(secret),
        delegation=HubDelegation(secret, TENANT),
        principals=principals,
        repository=repository,
        registry=registry,
        directory=directory,
        secrets=secrets,
        admin=admin,
        tokens=tokens,
        secret=secret,
        hub=hub,
    )


async def _administrator(hub: httpx.AsyncClient) -> dict[str, str]:
    """The administrator the hub made, logged in: with a first password that has to be changed
    before anything else is allowed, as the hub says."""
    first = LoginResult.model_validate(
        (await hub.post("/v1/auth/login", json=_login(FIRST_PASSWORD))).json()
    )
    assert first.must_change_password
    changed = await hub.post(
        "/v1/auth/change-password",
        json={"current_password": FIRST_PASSWORD, "new_password": ADMIN_PASSWORD},
        headers={"authorization": f"Bearer {first.token}"},
    )
    assert changed.status_code == 204, changed.text
    logged_in = LoginResult.model_validate(
        (await hub.post("/v1/auth/login", json=_login(ADMIN_PASSWORD))).json()
    )
    assert not logged_in.must_change_password
    return {"authorization": f"Bearer {logged_in.token}"}


def _login(password: str) -> dict[str, str]:
    return LoginRequest(username="admin", password=password).model_dump(mode="json")


async def _pair(
    hub: httpx.AsyncClient, admin: dict[str, str], home: Home, models: list[ModelBinding]
) -> dict[str, str]:
    """What the example files say, in the hub: the devices (each is given a credential, shown once),
    the skills bound to the external ones, the models, and the secret of the home."""

    async def post(path: str, body: object, *, expected: int) -> httpx.Response:
        sent = await hub.post(path, json=body, headers=admin)
        assert sent.status_code == expected, f"{path}: {sent.status_code} {sent.text}"
        return sent

    tokens: dict[str, str] = {}
    for system in (record for record, _ in home.system):
        paired = await post("/v1/devices", _system_device(system), expected=201)
        tokens[system.hardware_id] = PairedDevice.model_validate(paired.json()).token

    external_ids = {}
    for external in home.external:
        made = await post("/v1/devices", _external_device(external), expected=201)
        external_ids[external.name] = ExternalDeviceRecord.model_validate(made.json()).id
    for spec in home.bindings:
        request = CreateBindingRequest(
            skill=spec.skill,
            device_id=external_ids[spec.device_name],
            params=spec.params,
            enabled=spec.enabled,
        )
        await post("/v1/bindings", request.model_dump(mode="json"), expected=201)

    for model in models:
        request_for_model = CreateModelBindingRequest(
            role=model.role,
            provider=model.provider,
            model=model.model,
            base_url=model.base_url,
            secret_ref=model.secret_ref,
            options=model.options,
        )
        await post("/v1/models", request_for_model.model_dump(mode="json"), expected=200)

    await post(
        "/v1/secrets",
        SetSecretRequest(name="HA_TOKEN", value=HA_TOKEN).model_dump(mode="json"),
        expected=200,
    )
    return tokens


def _owner(record: SystemDeviceRecord | ExternalDeviceRecord) -> DeviceOwner:
    return DeviceOwner.USER if record.owner.kind is OwnerKind.USER else DeviceOwner.TENANT


def _system_device(record: SystemDeviceRecord) -> object:
    return CreateSystemDeviceRequest(
        name=record.name,
        hardware_id=record.hardware_id,
        owner=_owner(record),
        capabilities=record.capabilities,
        display=record.display,
        aliases=record.aliases,
        tags=record.tags,
        policy=record.policy,
    ).model_dump(mode="json")


def _external_device(record: ExternalDeviceRecord) -> object:
    return CreateExternalDeviceRequest(
        name=record.name,
        owner=_owner(record),
        sensitivity=record.sensitivity,
        external_refs=record.external_refs,
        aliases=record.aliases,
        tags=record.tags,
        policy=record.policy,
    ).model_dump(mode="json")
