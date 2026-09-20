import logging
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send

from factotum.contracts.errors import (
    AuthError,
    BindingNotFoundError,
    CapabilityNotSupportedError,
    CollectionNotFoundError,
    ConflictError,
    ContentFilteredError,
    ContextTooLongError,
    DocumentNotFoundError,
    DocumentTooLargeError,
    EmbeddingNotConfiguredError,
    FactotumError,
    GatewayError,
    GatewayUnavailableError,
    InvalidRequestError,
    InvalidSchemaError,
    InvalidStructuredOutputError,
    NotFoundError,
    PolicyViolationError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitedError,
    RoleNotSupportedError,
    SecretNotFoundError,
    UnsupportedDocumentError,
    UrlNotAllowedError,
)
from factotum.contracts.identity import Principal
from factotum.contracts.testing import make_tenant_id, make_tenant_principal, make_user_principal
from factotum.web import BodySizeLimit, Caller, create_base_app, error_body, status_for
from factotum.web.api import register_status

SECRET = "sk-live-0123456789abcdefghijklmnop"


class Note(BaseModel):
    text: str


class Fixed:
    """An authenticator that knows one principal, and refuses a header that says otherwise."""

    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        if headers.get("x-refuse"):
            raise AuthError("no credentials")
        return self.principal


def build(**options: Any) -> httpx.AsyncClient:
    principal = make_user_principal(tenant_id=make_tenant_id())
    app = create_base_app("test", Fixed(principal), **options)

    @app.get("/who")
    async def who(caller: Caller) -> dict[str, str]:
        return {"tenant": str(caller.tenant_id)}

    @app.post("/note")
    async def note(body: Note, caller: Caller) -> dict[str, str]:
        return {"text": body.text}

    @app.get("/fail/{name}")
    async def fail(name: str) -> None:
        raise ERRORS[name]

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"boom with {SECRET} inside")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://service")


ERRORS: dict[str, FactotumError] = {
    "rate": RateLimitedError("slow down", retry_after_s=3.2),
    "rate-nohint": RateLimitedError("slow down"),
    "detail": InvalidRequestError("bad", details={"status": 404}),
}


class TestStatusTable:
    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (AuthError("no"), 401, "auth_error"),
            (PolicyViolationError("no"), 403, "policy_violation"),
            (BindingNotFoundError("no"), 404, "binding_not_found"),
            (ContextTooLongError("too long"), 413, "context_too_long"),
            (InvalidSchemaError("bad"), 422, "invalid_schema"),
            (InvalidRequestError("bad"), 422, "invalid_request"),
            (RoleNotSupportedError("no"), 422, "role_not_supported"),
            (ContentFilteredError("no"), 422, "content_filtered"),
            (RateLimitedError("slow"), 429, "rate_limited"),
            (ProviderTimeoutError("late"), 504, "provider_timeout"),
            (ProviderAuthError("key"), 502, "provider_auth_error"),
            (ProviderUnavailableError("down"), 502, "provider_unavailable"),
            (InvalidStructuredOutputError("bad"), 502, "invalid_structured_output"),
            (SecretNotFoundError("no"), 502, "secret_not_found"),
            (UrlNotAllowedError("no"), 502, "url_not_allowed"),
            (ProviderError("other"), 502, "provider_error"),
            (NotFoundError("no"), 404, "not_found"),
            (CollectionNotFoundError("no"), 404, "collection_not_found"),
            (DocumentNotFoundError("no"), 404, "document_not_found"),
            (ConflictError("no"), 409, "conflict"),
            (EmbeddingNotConfiguredError("no"), 409, "embedding_not_configured"),
            (UnsupportedDocumentError("no"), 415, "unsupported_document"),
            (DocumentTooLargeError("no"), 413, "document_too_large"),
            (GatewayError("no"), 502, "gateway_error"),
            (GatewayUnavailableError("no"), 503, "gateway_unavailable"),
            (CapabilityNotSupportedError("odd"), 500, "capability_not_supported"),
            (FactotumError("odd"), 500, "factotum_error"),
        ],
    )
    async def test_each_error_has_its_status_and_keeps_its_code(
        self, error: FactotumError, status: int, code: str
    ) -> None:
        ERRORS["under-test"] = error

        async with build() as client:
            response = await client.get("/fail/under-test")

        assert response.status_code == status
        assert response.json()["error"]["code"] == code

    def test_an_error_without_a_registered_status_gets_the_one_of_its_closest_parent(self) -> None:
        class OddError(ProviderUnavailableError):
            pass

        assert status_for(OddError("odd")) == 502

    def test_a_service_can_register_the_errors_it_adds(self) -> None:
        class ShelfEmptyError(FactotumError):
            code = "shelf_empty"

        register_status(ShelfEmptyError, 409)

        assert status_for(ShelfEmptyError("nothing here")) == 409

    async def test_the_message_and_the_details_are_passed_on(self) -> None:
        async with build() as client:
            response = await client.get("/fail/detail")

        assert response.json()["error"] == {
            "code": "invalid_request",
            "message": "bad",
            "details": {"status": 404},
        }

    async def test_a_rate_limit_tells_the_caller_when_to_come_back(self) -> None:
        async with build() as client:
            response = await client.get("/fail/rate")

        assert response.status_code == 429
        assert response.headers["retry-after"] == "4"

    async def test_a_rate_limit_without_a_hint_has_no_retry_after(self) -> None:
        async with build() as client:
            response = await client.get("/fail/rate-nohint")

        assert "retry-after" not in response.headers

    def test_the_error_body_has_one_shape(self) -> None:
        assert error_body("code", "message") == {
            "error": {"code": "code", "message": "message", "details": {}}
        }


class TestAuthentication:
    async def test_the_route_gets_the_principal_the_authenticator_built(self) -> None:
        async with build() as client:
            response = await client.get("/who")

        assert response.status_code == 200
        assert "tenant" in response.json()

    async def test_a_refused_request_is_a_401(self) -> None:
        async with build() as client:
            response = await client.get("/who", headers={"x-refuse": "1"})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "auth_error"

    async def test_authentication_comes_before_the_body_is_looked_at(self) -> None:
        async with build() as client:
            response = await client.post("/note", json={"nonsense": 1}, headers={"x-refuse": "1"})

        assert response.status_code == 401

    def test_a_tenant_principal_works_too(self) -> None:
        assert Fixed(make_tenant_principal()).authenticate({}).device_id is None


class TestInvalidBodies:
    async def test_a_body_that_breaks_the_contract_is_a_422_that_says_where(self) -> None:
        async with build() as client:
            response = await client.post("/note", json={"wrong": 1})

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "invalid_request_body"
        assert error["details"]["errors"][0]["loc"] == ["body", "text"]

    async def test_what_was_sent_is_never_echoed_back(self) -> None:
        async with build() as client:
            response = await client.post("/note", json={"text": 42, "secret": SECRET})

        assert response.status_code == 422
        assert SECRET not in response.text

    async def test_a_body_that_is_not_json_is_a_422(self) -> None:
        async with build() as client:
            response = await client.post(
                "/note", content=b"{not json", headers={"content-type": "application/json"}
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request_body"


class TestUnexpectedErrors:
    async def test_an_error_that_nobody_expected_is_a_500_that_explains_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="factotum.web"):
            async with build() as client:
                response = await client.get("/boom")

        assert response.status_code == 500
        assert response.json() == {
            "error": {"code": "internal_error", "message": "internal error", "details": {}}
        }
        assert SECRET not in response.text
        assert any("unhandled error" in record.getMessage() for record in caplog.records)


class TestDocumentation:
    async def test_the_docs_are_there_when_asked_for(self) -> None:
        async with build(docs=True) as client:
            page = await client.get("/docs")
            schema = await client.get("/openapi.json")

        assert page.status_code == 200
        assert "/note" in schema.json()["paths"]

    async def test_they_are_not_served_otherwise(self) -> None:
        async with build() as client:
            responses = [await client.get(path) for path in ("/docs", "/redoc", "/openapi.json")]

        assert [response.status_code for response in responses] == [404, 404, 404]


class TestBodySizeLimit:
    """The guard on its own, around an application that only says it was reached."""

    @staticmethod
    async def reached(limit: int, scope: Scope) -> tuple[bool, list[Message]]:
        seen: list[Message] = []
        was_reached = False

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal was_reached
            was_reached = True

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            seen.append(message)

        await BodySizeLimit(app, max_bytes=limit)(scope, receive, send)
        return was_reached, seen

    @staticmethod
    def http(length: bytes | None) -> Scope:
        headers = [] if length is None else [(b"content-length", length)]
        return {"type": "http", "headers": headers}

    async def test_a_body_at_the_limit_gets_through(self) -> None:
        was_reached, sent = await self.reached(10, self.http(b"10"))

        assert was_reached
        assert sent == []

    async def test_a_body_over_the_limit_is_refused_and_never_reaches_the_application(self) -> None:
        was_reached, sent = await self.reached(10, self.http(b"11"))

        assert not was_reached
        assert sent[0]["status"] == 413

    @pytest.mark.parametrize("length", [None, b"", b"many"])
    async def test_a_body_that_declares_no_usable_length_is_not_judged_here(
        self, length: bytes | None
    ) -> None:
        was_reached, _ = await self.reached(10, self.http(length))

        assert was_reached

    async def test_what_is_not_http_passes_through(self) -> None:
        was_reached, _ = await self.reached(10, {"type": "lifespan"})

        assert was_reached

    async def test_the_application_refuses_a_huge_declared_body_with_the_shared_error(self) -> None:
        async with build(max_body_bytes=100) as client:
            response = await client.post(
                "/note",
                content=b"{}",
                headers={"content-type": "application/json", "content-length": "101"},
            )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
