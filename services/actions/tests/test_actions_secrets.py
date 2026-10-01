"""The secrets a skill is given, and whose they are: a skill gets its owner's own and no others, and
nothing of them ever appears in what is said about them."""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from factotum.actions.secrets import (
    FileSecrets,
    HubSecretResolver,
    NoSecrets,
    load_secrets,
    parse_secrets,
)
from factotum.actions.skills import load_folder
from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DevPrincipalDelegation
from factotum.contracts.errors import AuthError, SecretNotFoundError
from factotum.contracts.hub import SecretList, SecretView
from factotum.contracts.identity import Owner, OwnerKind, Subject
from factotum.contracts.ids import TenantId
from factotum.contracts.testing import make_tenant_id, make_user_id

TEST = ServiceSettings(_env_file=None, environment=Environment.TEST)
TENANT = make_tenant_id()
USER = make_user_id()
OF_THE_TENANT = Owner.of_subject(Subject.tenant(TENANT))
OF_THE_USER = Owner.of_subject(Subject.user(USER))

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "config" / "secrets.example.yaml"
EXAMPLE_SKILLS = ROOT / "skills" / "examples"

FILE = """\
tenant:
  HA_TOKEN: "the-token-of-the-home"
  ROUTER_PASSWORD: "hunter2"
user:
  HA_TOKEN: "my-own-token"
  SPOTIFY_KEY: "sp-123"
"""


def secrets(text: str = FILE) -> FileSecrets:
    return FileSecrets(TEST, parse_secrets(text, tenant_id=TENANT, user_id=USER), tenant_id=TENANT)


class TestReadingTheFile:
    def test_the_secrets_are_the_tenants_and_the_users(self) -> None:
        held = parse_secrets(FILE, tenant_id=TENANT, user_id=USER)

        assert held[OF_THE_TENANT] == {
            "HA_TOKEN": "the-token-of-the-home",
            "ROUTER_PASSWORD": "hunter2",
        }
        assert held[OF_THE_USER] == {"HA_TOKEN": "my-own-token", "SPOTIFY_KEY": "sp-123"}

    @pytest.mark.parametrize("text", ["", "# nothing yet\n", "tenant:\nuser:\n", "tenant: {}"])
    def test_a_file_without_secrets_is_a_file_of_no_secrets(self, text: str) -> None:
        held = parse_secrets(text, tenant_id=TENANT, user_id=USER)

        assert held == {OF_THE_TENANT: {}, OF_THE_USER: {}}

    @pytest.mark.parametrize(
        ("text", "problem"),
        [
            ("- a\n- b\n", "expected a mapping"),
            ("just text\n", "expected a mapping"),
            ("device:\n  A: x\n", "unknown top-level keys"),
            ("tenant: [a, b]\n", "tenant is a mapping"),
            ("tenant:\n  A: 1234\n", "quote it"),
            ("tenant:\n  A: ''\n", "not empty"),
            ("user:\n  A:\n", "quote it"),
            ("tenant:\n  PATH: x\n", "cannot be the name of a secret"),
            ("tenant:\n  PYTHONPATH: x\n", "cannot be the name of a secret"),
            ("user:\n  FACTOTUM_KEY: x\n", "cannot be the name of a secret"),
            ("tenant:\n  1: x\n", "cannot be the name of a secret"),
        ],
    )
    def test_what_is_wrong_with_it_is_said_and_it_says_where(self, text: str, problem: str) -> None:
        with pytest.raises(ValueError, match=problem):
            parse_secrets(text, tenant_id=TENANT, user_id=USER, source="secrets.dev.yaml")

    def test_the_place_is_said_and_the_value_is_not(self) -> None:
        with pytest.raises(ValueError, match=r"secrets\.dev\.yaml") as raised:
            parse_secrets(
                "tenant:\n  HA_TOKEN: 987654321\n",
                tenant_id=TENANT,
                user_id=USER,
                source="secrets.dev.yaml",
            )

        assert "987654321" not in str(raised.value)

    def test_a_file_is_read_from_where_it_is(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.dev.yaml"
        path.write_text(FILE, encoding="utf-8", newline="\n")

        loaded = load_secrets(path, TEST, tenant_id=TENANT, user_id=USER)

        assert isinstance(loaded, FileSecrets)

    def test_a_file_that_is_not_there_is_not_a_file_of_no_secrets(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_secrets(tmp_path / "nowhere.yaml", TEST, tenant_id=TENANT, user_id=USER)

    def test_a_file_that_is_wrong_says_which(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.dev.yaml"
        path.write_text("device: {}\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match=r"secrets\.dev\.yaml"):
            load_secrets(path, TEST, tenant_id=TENANT, user_id=USER)


class TestTheExampleFile:
    def test_it_is_a_file_of_secrets(self) -> None:
        held = parse_secrets(EXAMPLE.read_text("utf-8"), tenant_id=TENANT, user_id=USER)

        assert set(held) == {OF_THE_TENANT, OF_THE_USER}

    def test_it_has_a_place_for_every_secret_that_the_example_skills_declare(self) -> None:
        held = parse_secrets(EXAMPLE.read_text("utf-8"), tenant_id=TENANT, user_id=USER)
        skills, problems = load_folder(EXAMPLE_SKILLS)
        declared = {secret for skill in skills for secret in skill.manifest.permissions.secrets}

        assert problems == []
        assert declared
        assert declared <= set(held[OF_THE_TENANT])


class TestWhoseTheyAre:
    async def test_a_skill_of_the_tenant_is_given_the_secrets_of_the_tenant(self) -> None:
        given = await secrets().resolve(TENANT, OF_THE_TENANT, ("HA_TOKEN", "ROUTER_PASSWORD"))

        assert given == {"HA_TOKEN": "the-token-of-the-home", "ROUTER_PASSWORD": "hunter2"}

    async def test_a_skill_of_a_person_is_given_theirs_and_never_the_tenants(self) -> None:
        given = await secrets().resolve(TENANT, OF_THE_USER, ("HA_TOKEN",))

        assert given == {"HA_TOKEN": "my-own-token"}

    async def test_a_skill_of_a_person_cannot_have_a_secret_that_only_the_tenant_has(self) -> None:
        with pytest.raises(SecretNotFoundError, match="ROUTER_PASSWORD"):
            await secrets().resolve(TENANT, OF_THE_USER, ("ROUTER_PASSWORD",))

    async def test_a_skill_of_the_tenant_cannot_have_a_secret_that_only_a_person_has(self) -> None:
        with pytest.raises(SecretNotFoundError, match="SPOTIFY_KEY"):
            await secrets().resolve(TENANT, OF_THE_TENANT, ("SPOTIFY_KEY",))

    async def test_the_skill_of_somebody_else_is_given_nothing(self) -> None:
        stranger = Owner(kind=OwnerKind.USER, id=uuid4())

        with pytest.raises(SecretNotFoundError, match="HA_TOKEN"):
            await secrets().resolve(TENANT, stranger, ("HA_TOKEN",))

    async def test_a_skill_of_a_device_is_given_nothing_that_is_not_its_own(self) -> None:
        device = Owner(kind=OwnerKind.DEVICE, id=uuid4())

        with pytest.raises(SecretNotFoundError, match="HA_TOKEN"):
            await secrets().resolve(TENANT, device, ("HA_TOKEN",))

    async def test_another_tenant_is_given_nothing_even_for_the_same_owner(self) -> None:
        with pytest.raises(SecretNotFoundError, match="HA_TOKEN"):
            await secrets().resolve(TenantId(uuid4()), OF_THE_TENANT, ("HA_TOKEN",))

    async def test_a_skill_that_asks_for_nothing_is_given_nothing(self) -> None:
        assert await secrets().resolve(TENANT, OF_THE_TENANT, ()) == {}

    async def test_only_what_was_asked_for_is_given(self) -> None:
        given = await secrets().resolve(TENANT, OF_THE_TENANT, ("HA_TOKEN",))

        assert set(given) == {"HA_TOKEN"}

    async def test_a_secret_that_is_not_set_is_named_and_no_value_is_said(self) -> None:
        with pytest.raises(SecretNotFoundError) as raised:
            await secrets().resolve(TENANT, OF_THE_TENANT, ("HA_TOKEN", "OTHER"))

        assert raised.value.message == "the secret OTHER is not set"
        assert "the-token-of-the-home" not in str(raised.value)

    async def test_what_it_gives_can_be_changed_without_changing_what_it_keeps(self) -> None:
        held = secrets()
        first = await held.resolve(TENANT, OF_THE_TENANT, ("HA_TOKEN",))
        first["HA_TOKEN"] = "changed"

        again = await held.resolve(TENANT, OF_THE_TENANT, ("HA_TOKEN",))

        assert again == {"HA_TOKEN": "the-token-of-the-home"}


class TestWhereTheyAreKept:
    def test_a_file_of_secrets_does_not_exist_in_production(self) -> None:
        production = ServiceSettings(_env_file=None, environment=Environment.PRODUCTION)

        with pytest.raises(AuthError, match="disabled"):
            FileSecrets(production, {}, tenant_id=TENANT)

    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_it_exists_in_development_and_in_tests(self, environment: Environment) -> None:
        FileSecrets(ServiceSettings(_env_file=None, environment=environment), {}, tenant_id=TENANT)

    async def test_a_deployment_that_keeps_none_gives_none(self) -> None:
        assert await NoSecrets().resolve(TENANT, OF_THE_TENANT, ()) == {}
        with pytest.raises(SecretNotFoundError, match="HA_TOKEN"):
            await NoSecrets().resolve(TENANT, OF_THE_TENANT, ("HA_TOKEN",))


class Hub:
    """A hub that records what it is asked and answers with the secrets it is told to."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.secrets = SecretList()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return httpx.Response(200, content=self.secrets.model_dump_json())

    def resolver(self, tenant_id: TenantId = TENANT) -> HubSecretResolver:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://hub.test")
        return HubSecretResolver(client, DevPrincipalDelegation(TEST), tenant_id)


class TestTheHubResolver:
    async def test_it_knows_nothing_before_it_is_loaded(self) -> None:
        resolver = Hub().resolver()

        with pytest.raises(SecretNotFoundError, match="HA_TOKEN"):
            await resolver.resolve(TENANT, OF_THE_TENANT, ("HA_TOKEN",))

    async def test_reload_asks_the_hub_for_the_tenants_secrets_as_the_tenant(self) -> None:
        hub = Hub()
        resolver = hub.resolver()

        await resolver.reload()

        [request] = hub.sent
        assert request.url.path == "/v1/secrets"
        assert request.headers["x-factotum-dev-subject"] == "tenant"

    async def test_a_skill_of_the_tenant_is_given_the_secrets_of_the_tenant_once_loaded(
        self,
    ) -> None:
        hub = Hub()
        hub.secrets = SecretList(
            secrets=(SecretView(name="HA_TOKEN", owner=OF_THE_TENANT, value="the-token-of-home"),)
        )
        resolver = hub.resolver()
        await resolver.reload()

        given = await resolver.resolve(TENANT, OF_THE_TENANT, ("HA_TOKEN",))

        assert given == {"HA_TOKEN": "the-token-of-home"}

    async def test_a_skill_of_a_person_cannot_have_a_secret_that_only_the_tenant_has(self) -> None:
        hub = Hub()
        hub.secrets = SecretList(
            secrets=(SecretView(name="HA_TOKEN", owner=OF_THE_TENANT, value="x"),)
        )
        resolver = hub.resolver()
        await resolver.reload()

        with pytest.raises(SecretNotFoundError, match="HA_TOKEN"):
            await resolver.resolve(TENANT, OF_THE_USER, ("HA_TOKEN",))

    async def test_another_tenant_is_given_nothing_even_for_the_same_owner(self) -> None:
        hub = Hub()
        hub.secrets = SecretList(
            secrets=(SecretView(name="HA_TOKEN", owner=OF_THE_TENANT, value="x"),)
        )
        resolver = hub.resolver()
        await resolver.reload()

        with pytest.raises(SecretNotFoundError, match="HA_TOKEN"):
            await resolver.resolve(make_tenant_id(), OF_THE_TENANT, ("HA_TOKEN",))

    async def test_reloading_again_replaces_what_was_there_before(self) -> None:
        hub = Hub()
        hub.secrets = SecretList(
            secrets=(SecretView(name="HA_TOKEN", owner=OF_THE_TENANT, value="first"),)
        )
        resolver = hub.resolver()
        await resolver.reload()

        hub.secrets = SecretList(
            secrets=(SecretView(name="ROUTER_PASSWORD", owner=OF_THE_TENANT, value="second"),)
        )
        await resolver.reload()

        with pytest.raises(SecretNotFoundError, match="HA_TOKEN"):
            await resolver.resolve(TENANT, OF_THE_TENANT, ("HA_TOKEN",))
        given = await resolver.resolve(TENANT, OF_THE_TENANT, ("ROUTER_PASSWORD",))
        assert given == {"ROUTER_PASSWORD": "second"}

    async def test_a_hub_that_answers_with_an_error_is_not_swallowed(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url="http://hub.test"
        )
        resolver = HubSecretResolver(client, DevPrincipalDelegation(TEST), TENANT)

        with pytest.raises(httpx.HTTPStatusError):
            await resolver.reload()
