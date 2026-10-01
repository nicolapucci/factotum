"""The HTTP interface of the hub, with the service behind it made of a store in memory."""

from typing import Any

import httpx
from fastapi import FastAPI

from factotum.common.hubauth import HubAuthenticator
from factotum.common.tokens import MIN_SECRET_BYTES
from factotum.hub.api import Operations, create_app
from factotum.hub.service import HubService
from factotum.hub.store.memory import InMemoryStore

SECRET = b"k" * MIN_SECRET_BYTES
USERNAME = "admin"
PASSWORD = "the-first-password"


async def app_of(*, ready: Any = None) -> FastAPI:
    service = HubService(InMemoryStore(), secret=SECRET, session_hours=1)
    await service.bootstrap(username=USERNAME, password=PASSWORD)
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
