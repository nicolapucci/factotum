"""The HTTP interface of the actions service, with the service behind it made of doubles."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from factotum.actions.api import MAX_BODY_BYTES, Operations, create_app
from factotum.actions.audit import AuditReader
from factotum.actions.catalog import Catalog
from factotum.actions.rig import TEST_SETTINGS, ActionsRig, plan_of
from factotum.actions.testing import LAMP
from factotum.common.devauth import DevAuthenticator, dev_headers
from factotum.contracts.actions import ActOutcome
from factotum.contracts.identity import Role

WEATHER = """\
---
name: weather
description: Say what the weather is like somewhere.
examples:
  - what is the weather in Rome
effect: read_only
permissions:
  network: [api.example.org]
---
Ask the forecast service.
"""


def app_of(
    rig: ActionsRig,
    *,
    roles: frozenset[Role] = frozenset({Role.ADMIN}),
    folder: Path | None = None,
    docs: bool = False,
    ready: Any = None,
) -> FastAPI:
    operations = Operations(
        service=rig.service,
        catalog=Catalog(rig.store, rig.index, folder=folder, now=rig.clock),
        audit=AuditReader(rig.store),
    )
    authenticator = DevAuthenticator(
        TEST_SETTINGS, tenant_id=rig.tenant, user_id=rig.user, roles=roles
    )
    return create_app(authenticator, operations=operations, docs=docs, ready=ready)


def client_of(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://actions")


def at(rig: ActionsRig, device: str) -> dict[str, str]:
    """The headers of a call that a device makes."""
    return dev_headers(rig.principal(device))


def error_code(response: httpx.Response) -> str:
    code: str = response.json()["error"]["code"]
    return code


class TestLiveness:
    async def test_it_says_that_it_is_alive_and_needs_no_credentials_to_say_so(self) -> None:
        async with client_of(app_of(ActionsRig())) as http:
            response = await http.get("/healthz")

        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    async def test_it_is_ready_when_what_it_depends_on_answers(self) -> None:
        async def answers() -> bool:
            return True

        async with client_of(app_of(ActionsRig(), ready=answers)) as http:
            response = await http.get("/readyz")

        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    async def test_it_is_not_ready_when_it_does_not(self) -> None:
        async def does_not() -> bool:
            return False

        async with client_of(app_of(ActionsRig(), ready=does_not)) as http:
            response = await http.get("/readyz")

        assert (response.status_code, response.json()) == (503, {"status": "unavailable"})

    async def test_with_nothing_to_ask_it_is_ready(self) -> None:
        async with client_of(app_of(ActionsRig())) as http:
            assert (await http.get("/readyz")).status_code == 200

    @pytest.mark.parametrize(("docs", "status"), [(False, 404), (True, 200)])
    async def test_the_documentation_is_there_only_when_it_is_asked_for(
        self, docs: bool, status: int
    ) -> None:
        async with client_of(app_of(ActionsRig(), docs=docs)) as http:
            assert (await http.get("/docs")).status_code == status


class TestActing:
    async def test_a_message_that_asks_for_something_is_done_and_the_answer_is_the_outcome(
        self,
    ) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))

        async with client_of(app_of(rig)) as http:
            response = await http.post(
                "/v1/act",
                json={"message": "turn on the living room lamp"},
                headers=at(rig, "Kitchen speaker"),
            )

        outcome = ActOutcome.model_validate(response.json())
        assert response.status_code == 200
        assert (outcome.status.value, outcome.skill) == ("done", "lamp")
        assert outcome.reply
        assert [request.skill_name for request in rig.runner.requests] == ["lamp"]

    async def test_a_message_that_asks_for_nothing_says_so_and_nothing_else(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)

        async with client_of(app_of(rig)) as http:
            response = await http.post(
                "/v1/act",
                json={"message": "explain photosynthesis to me slowly"},
                headers=at(rig, "Kitchen speaker"),
            )

        assert response.status_code == 200
        assert response.json() == {
            "status": "no_action",
            "reply": None,
            "code": None,
            "skill": None,
            "reasons": [],
            "audit_id": None,
            "degraded": False,
        }

    async def test_a_message_that_untrusted_content_is_in_is_carried_to_the_decision(self) -> None:
        rig = ActionsRig()
        await rig.install(LAMP)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))

        async with client_of(app_of(rig)) as http:
            response = await http.post(
                "/v1/act",
                json={"message": "turn on the living room lamp", "untrusted_content_in_turn": True},
                headers=at(rig, "Kitchen speaker"),
            )

        assert response.json()["status"] == "needs_confirmation"
        assert rig.runner.requests == []

    @pytest.mark.parametrize(
        "body",
        [{"message": ""}, {}, {"message": "hello", "surprise": 1}, {"message": "x" * 5000}],
    )
    async def test_a_request_that_is_not_one_is_refused_without_being_echoed(
        self, body: dict[str, Any]
    ) -> None:
        async with client_of(app_of(ActionsRig())) as http:
            response = await http.post("/v1/act", json=body)

        assert response.status_code == 422
        assert error_code(response) == "invalid_request_body"
        assert "x" * 100 not in response.text

    async def test_a_caller_that_is_not_known_is_refused(self) -> None:
        async with client_of(app_of(ActionsRig())) as http:
            response = await http.post(
                "/v1/act",
                json={"message": "hello"},
                headers={"x-factotum-dev-subject": "somebody else"},
            )

        assert (response.status_code, error_code(response)) == (401, "auth_error")


class TestTheSkills:
    async def test_a_skill_is_installed_then_reviewed_and_enabled_then_stopped_then_deleted(
        self,
    ) -> None:
        rig = ActionsRig()
        async with client_of(app_of(rig)) as http:
            installed = await http.post("/v1/skills", json={"source": WEATHER})
            skill = installed.json()
            url = f"/v1/skills/{skill['id']}"
            again = await http.post("/v1/skills", json={"source": WEATHER})
            enabled = await http.post(f"{url}/enable")
            listed = await http.get("/v1/skills")
            stopped = await http.post(f"{url}/disable")
            deleted = await http.delete(url)
            gone = await http.get(url)

        assert installed.status_code == 201
        assert (skill["name"], skill["enabled_version"], skill["granted"]) == (
            "weather",
            None,
            None,
        )
        assert (again.status_code, again.json()["id"]) == (200, skill["id"])
        assert enabled.status_code == 200
        assert enabled.json()["enabled_version"] == 1
        assert enabled.json()["granted"]["network"] == ["api.example.org"]
        assert [item["name"] for item in listed.json()["skills"]] == ["weather"]
        assert stopped.json()["enabled_version"] is None
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert (gone.status_code, error_code(gone)) == (404, "skill_not_found")

    async def test_a_version_can_be_named_when_it_is_enabled(self) -> None:
        rig = ActionsRig()
        async with client_of(app_of(rig)) as http:
            skill = (await http.post("/v1/skills", json={"source": WEATHER})).json()
            wider = WEATHER.replace("[api.example.org]", "[api.example.org, b.org]")
            await http.post("/v1/skills", json={"source": wider})

            newest = await http.post(f"/v1/skills/{skill['id']}/enable", json={})
            back = await http.post(f"/v1/skills/{skill['id']}/enable", json={"version": 1})
            missing = await http.post(f"/v1/skills/{skill['id']}/enable", json={"version": 9})

        assert newest.json()["enabled_version"] == 2
        assert back.json()["enabled_version"] == 1
        assert (missing.status_code, error_code(missing)) == (404, "skill_not_found")

    async def test_a_skill_that_is_not_one_is_refused_and_says_why(self) -> None:
        async with client_of(app_of(ActionsRig())) as http:
            response = await http.post("/v1/skills", json={"source": "just text"})

        assert (response.status_code, error_code(response)) == (422, "skill_invalid")
        assert "three dashes" in response.json()["error"]["message"]

    async def test_a_skill_that_does_not_exist_is_not_found_and_an_id_that_is_not_one_is_refused(
        self,
    ) -> None:
        async with client_of(app_of(ActionsRig())) as http:
            missing = await http.get("/v1/skills/6ba7b810-9dad-41d1-80b4-00c04fd430c8")
            garbled = await http.get("/v1/skills/not-an-id")

        assert (missing.status_code, error_code(missing)) == (404, "skill_not_found")
        assert (garbled.status_code, error_code(garbled)) == (422, "invalid_request_body")

    async def test_a_member_does_not_install_a_skill_for_the_tenant(self) -> None:
        async with client_of(app_of(ActionsRig(), roles=frozenset({Role.MEMBER}))) as http:
            response = await http.post("/v1/skills", json={"source": WEATHER})

        assert (response.status_code, error_code(response)) == (403, "policy_violation")

    async def test_a_member_installs_one_of_their_own(self) -> None:
        async with client_of(app_of(ActionsRig(), roles=frozenset({Role.MEMBER}))) as http:
            response = await http.post("/v1/skills", json={"source": WEATHER, "owner": "user"})

        assert response.status_code == 201
        assert response.json()["owner"]["kind"] == "user"

    async def test_nothing_of_the_catalog_is_served_through_a_device(self) -> None:
        rig = ActionsRig()
        async with client_of(app_of(rig)) as http:
            skill = (await http.post("/v1/skills", json={"source": WEATHER})).json()
            headers = at(rig, "My phone")
            responses = [
                await http.get("/v1/skills", headers=headers),
                await http.post("/v1/skills", json={"source": WEATHER}, headers=headers),
                await http.get(f"/v1/skills/{skill['id']}", headers=headers),
                await http.post(f"/v1/skills/{skill['id']}/enable", headers=headers),
                await http.post(f"/v1/skills/{skill['id']}/disable", headers=headers),
                await http.delete(f"/v1/skills/{skill['id']}", headers=headers),
                await http.post("/v1/skills/reload", headers=headers),
            ]

        assert [response.status_code for response in responses] == [403] * 7
        assert {error_code(response) for response in responses} == {"policy_violation"}

    async def test_reading_the_folder_again_needs_one(self, tmp_path: Path) -> None:
        rig = ActionsRig()
        (tmp_path / "weather").mkdir()
        (tmp_path / "weather" / "SKILL.md").write_text(WEATHER, encoding="utf-8", newline="\n")

        async with client_of(app_of(rig)) as http:
            without = await http.post("/v1/skills/reload")
        async with client_of(app_of(rig, folder=tmp_path)) as http:
            read = await http.post("/v1/skills/reload")

        assert (without.status_code, error_code(without)) == (409, "conflict")
        assert read.status_code == 200
        assert read.json()["installed"] == ["weather@1"]

    async def test_a_body_larger_than_a_skill_can_be_is_refused_before_it_is_read(self) -> None:
        async with client_of(app_of(ActionsRig())) as http:
            response = await http.post(
                "/v1/skills", content=b"x" * (MAX_BODY_BYTES + 1), headers={"content-type": "x"}
            )

        assert (response.status_code, error_code(response)) == (413, "payload_too_large")


class TestTheAuditTrail:
    async def acted(self, rig: ActionsRig) -> None:
        await rig.install(LAMP)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "on"}))
        await rig.hear("Kitchen speaker", "turn on the living room lamp")

    async def test_what_was_done_is_read_by_an_administrator_the_latest_first(self) -> None:
        rig = ActionsRig()
        await self.acted(rig)
        rig.clock.advance(minutes=1)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "off"}))
        await rig.hear("Kitchen speaker", "turn off the living room lamp")

        async with client_of(app_of(rig)) as http:
            response = await http.get("/v1/audit")

        records = response.json()["records"]
        assert response.status_code == 200
        assert [record["params"]["state"] for record in records] == ["off", "on"]
        assert {record["skill_name"] for record in records} == {"lamp"}
        assert response.json()["next_before"] is None

    async def test_a_page_at_a_time_and_by_skill_and_by_device(self) -> None:
        rig = ActionsRig()
        await self.acted(rig)
        rig.clock.advance(minutes=1)
        rig.plans(plan_of("lamp", device="Living room lamp", parameters={"state": "off"}))
        await rig.hear("Kitchen speaker", "turn off the living room lamp")
        kitchen = rig.device("Kitchen speaker").id

        async with client_of(app_of(rig)) as http:
            first = (await http.get("/v1/audit", params={"limit": 1})).json()
            second = (
                await http.get("/v1/audit", params={"limit": 1, "before": first["next_before"]})
            ).json()
            other_skill = (await http.get("/v1/audit", params={"skill": "garage-door"})).json()
            other_device = (
                await http.get("/v1/audit", params={"device": str(rig.device("My phone").id)})
            ).json()
            this_device = (await http.get("/v1/audit", params={"device": str(kitchen)})).json()

        assert first["next_before"] is not None
        assert (len(first["records"]), second["next_before"]) == (1, None)
        assert first["records"][0]["id"] != second["records"][0]["id"]
        assert other_skill["records"] == []
        assert other_device["records"] == []
        assert len(this_device["records"]) == 2

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"before": "yesterday"}])
    async def test_a_page_that_makes_no_sense_is_refused(self, params: dict[str, Any]) -> None:
        async with client_of(app_of(ActionsRig())) as http:
            response = await http.get("/v1/audit", params=params)

        assert (response.status_code, error_code(response)) == (422, "invalid_request_body")

    async def test_a_member_does_not_read_it_and_neither_does_a_device(self) -> None:
        rig = ActionsRig()
        await self.acted(rig)

        async with client_of(app_of(rig, roles=frozenset({Role.MEMBER}))) as http:
            member = await http.get("/v1/audit")
        async with client_of(app_of(rig)) as http:
            device = await http.get("/v1/audit", headers=at(rig, "My phone"))

        assert (member.status_code, device.status_code) == (403, 403)

    async def test_the_time_of_what_is_read_is_the_time_it_was_done(self) -> None:
        rig = ActionsRig()
        await self.acted(rig)
        later = rig.clock.advance(minutes=5) + timedelta(minutes=1)

        async with client_of(app_of(rig)) as http:
            before = await http.get("/v1/audit", params={"before": later.isoformat()})
            long_ago = await http.get(
                "/v1/audit", params={"before": (later - timedelta(days=1)).isoformat()}
            )

        assert len(before.json()["records"]) == 1
        assert long_ago.json()["records"] == []
