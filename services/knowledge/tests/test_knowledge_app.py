"""The service put together from its settings: what it refuses to start without, and, against a
real database and a gateway that answers, that it comes up, reads a document and finds it again."""

import asyncio
import json

import httpx
import pytest
import respx
from pydantic import AnyHttpUrl

from factotum.common.config import Environment
from factotum.common.tokens import MIN_SECRET_BYTES
from factotum.contracts.errors import AuthError
from factotum.db.pool import Database
from factotum.db.settings import DatabaseSettings
from factotum.knowledge.app import build_app
from factotum.knowledge.settings import KnowledgeSettings
from factotum.knowledge.testing import FakeEmbedder, wipe

GATEWAY = "http://gateway.test"
SECRET = "s" * MIN_SECRET_BYTES
TEXT = (
    "To restore the factory settings of the router, hold the reset button for ten seconds "
    "until the light blinks. The warranty covers manufacturing defects for two years."
)


def settings_for(database: DatabaseSettings, **values: object) -> KnowledgeSettings:
    return KnowledgeSettings(
        _env_file=None,
        environment=Environment.TEST,
        postgres_host=database.postgres_host,
        postgres_port=database.postgres_port,
        postgres_user=database.postgres_user,
        postgres_password=database.postgres_password,
        postgres_db=database.postgres_db,
        knowledge_gateway_url=AnyHttpUrl(GATEWAY),
        knowledge_workers=1,
        **values,  # type: ignore[arg-type]
    )


class TestStartingUp:
    def test_it_does_not_start_in_production(self) -> None:
        with pytest.raises(AuthError, match="disabled"):
            build_app(KnowledgeSettings(_env_file=None, environment=Environment.PRODUCTION))

    def test_it_starts_in_production_once_the_hub_is_configured(self) -> None:
        app = build_app(
            KnowledgeSettings(
                _env_file=None,
                environment=Environment.PRODUCTION,
                hub_token_secret=SECRET,  # type: ignore[arg-type]
            )
        )

        assert {"/v1/search", "/healthz", "/readyz"} <= {route.path for route in app.routes}  # type: ignore[attr-defined]

    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_it_starts_in_development_and_in_tests(self, environment: Environment) -> None:
        app = build_app(KnowledgeSettings(_env_file=None, environment=environment))

        assert {"/v1/search", "/healthz", "/readyz"} <= {route.path for route in app.routes}  # type: ignore[attr-defined]

    def test_the_documentation_is_there_for_developers(self) -> None:
        app = build_app(KnowledgeSettings(_env_file=None, environment=Environment.DEVELOPMENT))

        assert app.docs_url == "/docs"

    async def test_without_a_database_it_does_not_come_up(self) -> None:
        app = build_app(
            KnowledgeSettings(
                _env_file=None,
                environment=Environment.TEST,
                postgres_port=1,
                postgres_password="x",  # type: ignore[arg-type]
            )
        )

        with pytest.raises(OSError):  # noqa: PT011 - what is refused is the connection
            async with app.router.lifespan_context(app):
                pass  # pragma: no cover - the database is not there


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
class TestAgainstARealDatabase:
    async def test_the_service_comes_up_reads_a_document_and_finds_it_again(
        self, database_settings: DatabaseSettings
    ) -> None:
        fake = FakeEmbedder(dimensions=32)

        def embeddings(request: httpx.Request) -> httpx.Response:
            inputs = json.loads(request.content)["inputs"]
            return httpx.Response(
                200,
                json={
                    "vectors": [list(fake.vector(text)) for text in inputs],
                    "dimensions": fake.dimensions,
                    "space_id": fake.space_id,
                },
            )

        app = build_app(settings_for(database_settings))
        with respx.mock(assert_all_called=False) as gateway:
            gateway.post(f"{GATEWAY}/v1/embeddings").mock(side_effect=embeddings)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://k") as client:
                    assert (await client.get("/readyz")).status_code == 200

                    created = await client.post("/v1/collections", json={"name": "Manuals"})
                    assert created.status_code == 201, created.text
                    collection = created.json()["id"]
                    assert created.json()["dimensions"] == 32

                    uploaded = await client.post(
                        f"/v1/collections/{collection}/documents",
                        files={"file": ("router.txt", TEXT.encode(), "text/plain")},
                    )
                    assert uploaded.status_code == 202, uploaded.text
                    document = uploaded.json()["id"]

                    status = "pending"
                    for _ in range(300):
                        read = await client.get(f"/v1/documents/{document}")
                        status = read.json()["status"]
                        if status in {"ready", "failed"}:
                            break
                        await asyncio.sleep(0.1)
                    assert status == "ready", read.text
                    assert read.json()["language"] == "en"

                    found = await client.post("/v1/search", json={"query": "reset the router"})
                    assert found.status_code == 200
                    [passage, *_] = found.json()["passages"]
                    assert "reset button" in passage["text"]
                    assert passage["matched_by"] == ["vector", "text"]
                    assert passage["untrusted"] is True

        database = await Database.connect(database_settings)
        try:
            await wipe(database)
        finally:
            await database.close()
