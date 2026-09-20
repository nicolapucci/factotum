import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import respx

from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.errors import (
    AuthError,
    ContextTooLongError,
    EmbeddingNotConfiguredError,
    GatewayError,
    GatewayUnavailableError,
    InvalidRequestError,
    ProviderUnavailableError,
    RateLimitedError,
)
from factotum.contracts.identity import Owner, OwnerKind
from factotum.model_client import (
    DevDelegation,
    Embedded,
    GatewayEmbedder,
    error_from,
)

URL = "http://gateway.test"
USER = Owner(kind=OwnerKind.USER, id=uuid4())
TENANT = Owner(kind=OwnerKind.TENANT, id=uuid4())


def settings(environment: Environment = Environment.TEST) -> ServiceSettings:
    return ServiceSettings(environment=environment, _env_file=None)


def answer(inputs: int, *, space: str = "emb-1", dimensions: int = 3) -> dict[str, object]:
    return {
        "vectors": [[float(n)] * dimensions for n in range(inputs)],
        "dimensions": dimensions,
        "space_id": space,
    }


def error(code: str, message: str = "it failed") -> dict[str, object]:
    return {"error": {"code": code, "message": message, "details": {}}}


@pytest.fixture
async def embedder() -> AsyncIterator[GatewayEmbedder]:
    async with httpx.AsyncClient(base_url=URL) as client:
        yield GatewayEmbedder(client, DevDelegation(settings()), batch=3)


class TestEmbedding:
    @respx.mock
    async def test_the_texts_go_to_the_gateway_and_the_vectors_come_back_in_order(
        self, embedder: GatewayEmbedder
    ) -> None:
        route = respx.post(f"{URL}/v1/embeddings").respond(200, json=answer(2))

        result = await embedder.embed(USER, ["first", "second"])

        assert result == Embedded(((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)), "emb-1", 3)
        assert json.loads(route.calls.last.request.content) == {"inputs": ["first", "second"]}

    @respx.mock
    async def test_it_acts_for_the_owner_it_is_given(self, embedder: GatewayEmbedder) -> None:
        route = respx.post(f"{URL}/v1/embeddings").respond(200, json=answer(1))

        await embedder.embed(USER, ["text"])
        await embedder.embed(TENANT, ["text"])

        subjects = [call.request.headers["x-factotum-dev-subject"] for call in route.calls]
        assert subjects == ["user", "tenant"]

    @respx.mock
    async def test_many_texts_are_sent_in_batches_and_put_back_together(
        self, embedder: GatewayEmbedder
    ) -> None:
        def reply(request: httpx.Request) -> httpx.Response:
            inputs = json.loads(request.content)["inputs"]
            return httpx.Response(
                200,
                json={
                    "vectors": [[float(text)] * 3 for text in inputs],
                    "dimensions": 3,
                    "space_id": "emb-1",
                },
            )

        route = respx.post(f"{URL}/v1/embeddings").mock(side_effect=reply)

        result = await embedder.embed(USER, [str(n) for n in range(7)])

        assert route.call_count == 3
        assert [vector[0] for vector in result.vectors] == [float(n) for n in range(7)]

    async def test_nothing_to_embed_is_a_mistake(self, embedder: GatewayEmbedder) -> None:
        with pytest.raises(ValueError, match="nothing to embed"):
            await embedder.embed(USER, [])

    @respx.mock
    async def test_a_model_that_changes_between_batches_is_refused(
        self, embedder: GatewayEmbedder
    ) -> None:
        respx.post(f"{URL}/v1/embeddings").mock(
            side_effect=[
                httpx.Response(200, json=answer(3, space="emb-1")),
                httpx.Response(200, json=answer(1, space="emb-2")),
            ]
        )

        with pytest.raises(GatewayError, match="changed"):
            await embedder.embed(USER, ["a", "b", "c", "d"])

    @respx.mock
    async def test_a_vector_too_few_is_refused(self, embedder: GatewayEmbedder) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(200, json=answer(1))

        with pytest.raises(GatewayError, match="too few or too many"):
            await embedder.embed(USER, ["a", "b"])

    @respx.mock
    @pytest.mark.parametrize("body", [{"vectors": "no"}, {}, {"vectors": [[1.0]], "dimensions": 2}])
    async def test_an_answer_that_is_not_an_embedding_is_refused(
        self, embedder: GatewayEmbedder, body: dict[str, object]
    ) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(200, json=body)

        with pytest.raises(GatewayError, match="not an embedding"):
            await embedder.embed(USER, ["a"])

    @respx.mock
    async def test_an_answer_that_is_not_json_is_refused(self, embedder: GatewayEmbedder) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(200, content=b"<html>hello</html>")

        with pytest.raises(GatewayError, match="not an embedding"):
            await embedder.embed(USER, ["a"])


class TestErrors:
    @respx.mock
    @pytest.mark.parametrize(
        ("status", "code", "kind"),
        [
            (413, "context_too_long", ContextTooLongError),
            (503, "provider_unavailable", ProviderUnavailableError),
            (422, "invalid_request", InvalidRequestError),
            (409, "embedding_not_configured", EmbeddingNotConfiguredError),
            (404, "binding_not_found", EmbeddingNotConfiguredError),
        ],
    )
    async def test_the_gateway_says_what_went_wrong_and_the_kind_survives_the_trip(
        self, embedder: GatewayEmbedder, status: int, code: str, kind: type[Exception]
    ) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(status, json=error(code))

        with pytest.raises(kind):
            await embedder.embed(USER, ["a"])

    @respx.mock
    async def test_a_rate_limit_says_when_to_come_back(self, embedder: GatewayEmbedder) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(
            429, json=error("rate_limited"), headers={"Retry-After": "7"}
        )

        with pytest.raises(RateLimitedError) as caught:
            await embedder.embed(USER, ["a"])

        assert caught.value.retry_after_s == 7.0

    @respx.mock
    async def test_a_rate_limit_that_does_not_say_when_is_still_one(
        self, embedder: GatewayEmbedder
    ) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(429, json=error("rate_limited"))

        with pytest.raises(RateLimitedError) as caught:
            await embedder.embed(USER, ["a"])

        assert caught.value.retry_after_s is None

    @respx.mock
    async def test_the_message_of_the_gateway_is_kept(self, embedder: GatewayEmbedder) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(413, json=error("context_too_long", "too long"))

        with pytest.raises(ContextTooLongError, match="too long"):
            await embedder.embed(USER, ["a"])

    @respx.mock
    async def test_a_code_it_does_not_know_is_a_gateway_error(
        self, embedder: GatewayEmbedder
    ) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(500, json=error("something_new", "odd"))

        with pytest.raises(GatewayError, match="odd") as caught:
            await embedder.embed(USER, ["a"])

        assert not isinstance(caught.value, GatewayUnavailableError)

    @respx.mock
    @pytest.mark.parametrize("status", [502, 503, 504])
    async def test_a_gateway_that_is_down_behind_a_proxy_is_unavailable(
        self, embedder: GatewayEmbedder, status: int
    ) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(status, content=b"<html>bad gateway</html>")

        with pytest.raises(GatewayUnavailableError):
            await embedder.embed(USER, ["a"])

    @respx.mock
    async def test_any_other_answer_without_a_code_is_a_gateway_error(
        self, embedder: GatewayEmbedder
    ) -> None:
        respx.post(f"{URL}/v1/embeddings").respond(418, content=b"teapot")

        with pytest.raises(GatewayError, match="418"):
            await embedder.embed(USER, ["a"])

    @respx.mock
    @pytest.mark.parametrize(
        "failure",
        [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ReadError("cut")],
    )
    async def test_a_gateway_that_cannot_be_reached_is_unavailable(
        self, embedder: GatewayEmbedder, failure: Exception
    ) -> None:
        respx.post(f"{URL}/v1/embeddings").mock(side_effect=failure)

        with pytest.raises(GatewayUnavailableError):
            await embedder.embed(USER, ["a"])

    def test_the_error_of_an_answer_without_json_is_judged_by_its_status(self) -> None:
        response = httpx.Response(500, content=b"boom")

        assert type(error_from(response)) is GatewayError


class TestDevDelegation:
    def test_it_does_not_exist_in_production(self) -> None:
        with pytest.raises(AuthError, match="disabled"):
            DevDelegation(settings(Environment.PRODUCTION))

    def test_a_collection_is_owned_by_a_user_or_the_tenant(self) -> None:
        delegation = DevDelegation(settings())

        with pytest.raises(ValueError, match="user or by the tenant"):
            delegation.headers(Owner(kind=OwnerKind.DEVICE, id=uuid4()))
