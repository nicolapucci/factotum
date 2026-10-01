"""The HTTP interface of the hub, with the service behind it made of a store in memory."""

from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI

from factotum.common.hubauth import HubAuthenticator
from factotum.common.tokens import MIN_SECRET_BYTES
from factotum.contracts.testing import make_tenant_id
from factotum.hub.api import Operations, create_app
from factotum.hub.service import HubService
from factotum.hub.store.memory import InMemoryStore

SECRET = b"k" * MIN_SECRET_BYTES
USERNAME = "admin"
PASSWORD = "the-first-password"


async def app_of(*, ready: Any = None) -> FastAPI:
    service = HubService(InMemoryStore(), secret=SECRET, session_hours=1)
    await service.bootstrap(tenant_id=make_tenant_id(), username=USERNAME, password=PASSWORD)
    return create_app(HubAuthenticator(SECRET), operations=Operations(service=service), ready=ready)


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")


def error_code(response: httpx.Response) -> str:
    code: str = response.json()["error"]["code"]
    return code


async def _login(http: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await http.post("/v1/auth/login", json={"username": username, "password": password})
    token: str = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


async def _admin_headers(http: httpx.AsyncClient) -> dict[str, str]:
    """An administrator past the forced password change, who may do anything an admin can."""
    headers = await _login(http, USERNAME, PASSWORD)
    await http.post(
        "/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "a new password"},
        headers=headers,
    )
    return await _login(http, USERNAME, "a new password")


class TestLiveness:
    async def test_it_says_it_is_alive_without_credentials(self) -> None:
        async with client_of(await app_of()) as http:
            response = await http.get("/healthz")

        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    async def test_it_is_ready_when_what_it_depends_on_answers(self) -> None:
        async def answers() -> bool:
            return True

        async with client_of(await app_of(ready=answers)) as http:
            response = await http.get("/readyz")

        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    async def test_it_is_not_ready_when_it_does_not(self) -> None:
        async def does_not() -> bool:
            return False

        async with client_of(await app_of(ready=does_not)) as http:
            response = await http.get("/readyz")

        assert (response.status_code, response.json()) == (503, {"status": "unavailable"})


class TestLoggingIn:
    async def test_the_bootstrap_administrator_can_log_in(self) -> None:
        async with client_of(await app_of()) as http:
            response = await http.post(
                "/v1/auth/login", json={"username": USERNAME, "password": PASSWORD}
            )

        assert response.status_code == 200
        body = response.json()
        assert body["must_change_password"] is True
        assert body["token"]

    async def test_the_wrong_password_is_refused(self) -> None:
        async with client_of(await app_of()) as http:
            response = await http.post(
                "/v1/auth/login", json={"username": USERNAME, "password": "wrong"}
            )

        assert (response.status_code, error_code(response)) == (401, "auth_error")


class TestAuthenticatedRoutes:
    async def test_no_token_is_refused(self) -> None:
        async with client_of(await app_of()) as http:
            response = await http.get("/v1/me")

        assert response.status_code == 401

    async def test_a_token_from_logging_in_reaches_me(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _login(http, USERNAME, PASSWORD)

            me = await http.get("/v1/me", headers=headers)

        assert (me.status_code, me.json()["username"]) == (200, USERNAME)


class TestChangingThePassword:
    async def test_it_needs_the_current_password(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _login(http, USERNAME, PASSWORD)

            response = await http.post(
                "/v1/auth/change-password",
                json={"current_password": "wrong", "new_password": "a new password"},
                headers=headers,
            )

        assert response.status_code == 401

    async def test_it_lets_the_administrator_log_in_with_the_new_one(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _login(http, USERNAME, PASSWORD)
            await http.post(
                "/v1/auth/change-password",
                json={"current_password": PASSWORD, "new_password": "a new password"},
                headers=headers,
            )

            again = await http.post(
                "/v1/auth/login", json={"username": USERNAME, "password": "a new password"}
            )

        assert (again.status_code, again.json()["must_change_password"]) == (200, False)


class TestAddingAUser:
    async def test_the_pending_administrator_must_change_the_password_first(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _login(http, USERNAME, PASSWORD)

            response = await http.post(
                "/v1/users", json={"username": "member", "password": "a password"}, headers=headers
            )

        assert (response.status_code, error_code(response)) == (403, "password_change_required")

    async def test_an_administrator_adds_a_member_who_may_not_add_another(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _login(http, USERNAME, PASSWORD)
            await http.post(
                "/v1/auth/change-password",
                json={"current_password": PASSWORD, "new_password": "a new password"},
                headers=headers,
            )
            headers = await _login(http, USERNAME, "a new password")

            created = await http.post(
                "/v1/users",
                json={"username": "member", "password": "member password"},
                headers=headers,
            )
            member_headers = await _login(http, "member", "member password")
            refused = await http.post(
                "/v1/users",
                json={"username": "another", "password": "a password"},
                headers=member_headers,
            )
            listed_by_member = await http.get("/v1/users", headers=member_headers)
            listed_by_admin = await http.get("/v1/users", headers=headers)

        assert (created.status_code, created.json()["roles"]) == (201, ["member"])
        assert (refused.status_code, error_code(refused)) == (403, "policy_violation")
        assert listed_by_member.status_code == 403
        usernames = sorted(user["username"] for user in listed_by_admin.json()["users"])
        assert usernames == ["admin", "member"]

    async def test_a_duplicate_username_is_a_conflict(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _login(http, USERNAME, PASSWORD)
            await http.post(
                "/v1/auth/change-password",
                json={"current_password": PASSWORD, "new_password": "a new password"},
                headers=headers,
            )
            headers = await _login(http, USERNAME, "a new password")

            response = await http.post(
                "/v1/users", json={"username": USERNAME, "password": "x" * 10}, headers=headers
            )

        assert (response.status_code, error_code(response)) == (409, "conflict")


SPEAKER_BODY = {
    "kind": "system",
    "name": "Kitchen speaker",
    "hardware_id": "kitchen-01",
    "capabilities": ["audio_out"],
}
LAMP_BODY = {"kind": "external", "name": "Living room lamp"}


class TestRegisteringADevice:
    async def test_the_pending_administrator_must_change_the_password_first(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _login(http, USERNAME, PASSWORD)

            response = await http.post("/v1/devices", json=SPEAKER_BODY, headers=headers)

        assert (response.status_code, error_code(response)) == (403, "password_change_required")

    async def test_a_system_device_is_paired_with_a_token(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)

            response = await http.post("/v1/devices", json=SPEAKER_BODY, headers=headers)

        assert response.status_code == 201
        body = response.json()
        assert body["device"]["name"] == "Kitchen speaker"
        assert body["token"]

    async def test_an_external_device_has_no_token(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)

            response = await http.post("/v1/devices", json=LAMP_BODY, headers=headers)

        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Living room lamp"
        assert "token" not in body

    async def test_a_member_may_not_register_a_device_but_may_list_them(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)
            await http.post("/v1/devices", json=LAMP_BODY, headers=headers)
            await http.post(
                "/v1/users", json={"username": "member", "password": "x" * 10}, headers=headers
            )
            member_headers = await _login(http, "member", "x" * 10)

            refused = await http.post("/v1/devices", json=LAMP_BODY, headers=member_headers)
            listed = await http.get("/v1/devices", headers=member_headers)

        assert (refused.status_code, error_code(refused)) == (403, "policy_violation")
        assert (listed.status_code, len(listed.json()["external"])) == (200, 1)

    async def test_a_duplicate_name_is_a_conflict(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)
            await http.post("/v1/devices", json=LAMP_BODY, headers=headers)

            response = await http.post("/v1/devices", json=LAMP_BODY, headers=headers)

        assert (response.status_code, error_code(response)) == (409, "conflict")


class TestBindingASkill:
    async def test_the_pending_administrator_must_change_the_password_first(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _login(http, USERNAME, PASSWORD)

            response = await http.post(
                "/v1/bindings", json={"skill": "lamp", "device_id": str(uuid4())}, headers=headers
            )

        assert (response.status_code, error_code(response)) == (403, "password_change_required")

    async def test_binding_an_unknown_device_is_not_found(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)

            response = await http.post(
                "/v1/bindings", json={"skill": "lamp", "device_id": str(uuid4())}, headers=headers
            )

        assert (response.status_code, error_code(response)) == (404, "not_found")

    async def test_a_binding_is_made_and_listed(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)
            lamp = (await http.post("/v1/devices", json=LAMP_BODY, headers=headers)).json()

            created = await http.post(
                "/v1/bindings",
                json={
                    "skill": "lamp",
                    "device_id": lamp["id"],
                    "params": {"entity_id": "light.living_room"},
                },
                headers=headers,
            )
            listed = await http.get("/v1/bindings", headers=headers)

        assert (created.status_code, created.json()["skill"]) == (201, "lamp")
        assert len(listed.json()["bindings"]) == 1

    async def test_the_same_skill_bound_twice_is_a_conflict(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)
            lamp = (await http.post("/v1/devices", json=LAMP_BODY, headers=headers)).json()
            await http.post(
                "/v1/bindings", json={"skill": "lamp", "device_id": lamp["id"]}, headers=headers
            )

            response = await http.post(
                "/v1/bindings", json={"skill": "lamp", "device_id": lamp["id"]}, headers=headers
            )

        assert (response.status_code, error_code(response)) == (409, "conflict")


OLLAMA_CHAT_BODY = {
    "role": "chat.main",
    "provider": "ollama",
    "model": "llama3.1:8b",
    "base_url": "http://localhost:11434",
}


class TestBindingAModel:
    async def test_the_pending_administrator_must_change_the_password_first(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _login(http, USERNAME, PASSWORD)

            response = await http.post("/v1/models", json=OLLAMA_CHAT_BODY, headers=headers)

        assert (response.status_code, error_code(response)) == (403, "password_change_required")

    async def test_a_model_is_bound_and_listed(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)

            created = await http.post("/v1/models", json=OLLAMA_CHAT_BODY, headers=headers)
            listed = await http.get("/v1/models", headers=headers)

        assert (created.status_code, created.json()["model"]) == (200, "llama3.1:8b")
        assert len(listed.json()["bindings"]) == 1

    async def test_binding_the_same_role_again_replaces_it(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)
            await http.post("/v1/models", json=OLLAMA_CHAT_BODY, headers=headers)

            replaced = await http.post(
                "/v1/models", json={**OLLAMA_CHAT_BODY, "model": "llama3.1:70b"}, headers=headers
            )
            listed = await http.get("/v1/models", headers=headers)

        assert replaced.status_code == 200
        assert [b["model"] for b in listed.json()["bindings"]] == ["llama3.1:70b"]

    async def test_a_member_may_not_bind_a_model_but_may_list(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)
            await http.post("/v1/models", json=OLLAMA_CHAT_BODY, headers=headers)
            await http.post(
                "/v1/users", json={"username": "member", "password": "x" * 10}, headers=headers
            )
            member_headers = await _login(http, "member", "x" * 10)

            refused = await http.post("/v1/models", json=OLLAMA_CHAT_BODY, headers=member_headers)
            listed = await http.get("/v1/models", headers=member_headers)

        assert (refused.status_code, error_code(refused)) == (403, "policy_violation")
        assert (listed.status_code, len(listed.json()["bindings"])) == (200, 1)

    async def test_a_hosted_provider_without_a_secret_ref_is_refused(self) -> None:
        async with client_of(await app_of()) as http:
            headers = await _admin_headers(http)

            response = await http.post(
                "/v1/models",
                json={"role": "chat.main", "provider": "openai", "model": "gpt-4o"},
                headers=headers,
            )

        assert response.status_code == 422
