"""The hub put together from its settings: what it refuses to start without, and, against a real
database, that the tenant and its administrator are made once and kept across a restart."""

import httpx
import pytest

from factotum.common.config import Environment
from factotum.common.tokens import MIN_SECRET_BYTES
from factotum.contracts.errors import AuthError
from factotum.db.settings import DatabaseSettings
from factotum.hub.app import build_app
from factotum.hub.settings import HubSettings

SECRET = "s" * MIN_SECRET_BYTES


def settings_for(database: DatabaseSettings, **values: object) -> HubSettings:
    return HubSettings(
        _env_file=None,
        environment=Environment.TEST,
        postgres_host=database.postgres_host,
        postgres_port=database.postgres_port,
        postgres_user=database.postgres_user,
        postgres_password=database.postgres_password,
        postgres_db=database.postgres_db,
        **values,  # type: ignore[arg-type]
    )


class TestStartingUp:
    @pytest.mark.parametrize(
        "environment", [Environment.DEVELOPMENT, Environment.TEST, Environment.PRODUCTION]
    )
    def test_it_never_starts_without_a_secret_of_its_own(self, environment: Environment) -> None:
        # Unlike every other service, the hub has no development fallback: it is the one that
        # mints, so development and test need a secret too, not only production.
        with pytest.raises(AuthError, match="token secret"):
            build_app(HubSettings(_env_file=None, environment=environment))

    def test_it_starts_with_a_secret(self) -> None:
        app = build_app(
            HubSettings(_env_file=None, environment=Environment.TEST, hub_token_secret=SECRET)  # type: ignore[arg-type]
        )

        paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
        assert {"/v1/auth/login", "/v1/me", "/v1/users", "/healthz", "/readyz"} <= paths

    def test_the_documentation_is_there_for_developers(self) -> None:
        app = build_app(
            HubSettings(
                _env_file=None,
                environment=Environment.DEVELOPMENT,
                hub_token_secret=SECRET,  # type: ignore[arg-type]
            )
        )

        assert app.docs_url == "/docs"

    def test_it_is_not_there_in_production(self) -> None:
        app = build_app(
            HubSettings(
                _env_file=None,
                environment=Environment.PRODUCTION,
                hub_token_secret=SECRET,  # type: ignore[arg-type]
            )
        )

        assert app.docs_url is None


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
class TestAgainstARealDatabase:
    async def test_the_administrator_is_made_once_and_kept_across_a_restart(
        self, database_settings: DatabaseSettings
    ) -> None:
        settings = settings_for(
            database_settings,
            hub_token_secret=SECRET,
            hub_admin_username="root",
            hub_admin_password="first-boot-password",
        )
        app = build_app(settings)

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as http,
        ):
            assert (await http.get("/readyz")).status_code == 200
            first = await http.post(
                "/v1/auth/login", json={"username": "root", "password": "first-boot-password"}
            )
        assert (first.status_code, first.json()["must_change_password"]) == (200, True)

        # Starting again (a restart) finds the tenant already there and does not touch it: the
        # same credentials still work, and a second administrator is not created.
        restarted = build_app(settings)
        async with (
            restarted.router.lifespan_context(restarted),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted), base_url="http://hub"
            ) as http,
        ):
            second = await http.post(
                "/v1/auth/login", json={"username": "root", "password": "first-boot-password"}
            )

        assert second.status_code == 200
