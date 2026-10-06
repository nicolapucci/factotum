import asyncio
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from factotum.common.devauth import DEV_TENANT_ID, DEVICE_HEADER, DevAuthenticator
from factotum.contracts.audio import wav_of
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.commands import (
    CommandResult,
    CommandStatus,
    PlayAudio,
    SetLed,
    SetVolume,
)
from factotum.contracts.edge import (
    MAX_FRAME_BYTES,
    Ack,
    CommandRequest,
    Feature,
)
from factotum.edge.api import create_app
from factotum.edge.testing import (
    TENANT,
    TEST,
    USER,
    Rig,
    make_device,
    quick_settings,
)


def app_for(rig: Rig, *, tenant: Any = TENANT) -> FastAPI:
    return create_app(
        DevAuthenticator(TEST, tenant_id=tenant, user_id=USER),
        sessions=rig.sessions,
        registry=rig.registry,
        conversation=rig.conversation,
        session_settings=rig.settings,
    )


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://edge")


class TestBeingAlive:
    async def test_it_is_alive(self) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            response = await http.get("/healthz")

            assert (response.status_code, response.json()) == (200, {"status": "ok"})

    @pytest.mark.parametrize(("answers", "status"), [(True, 200), (False, 503)])
    async def test_it_is_ready_when_the_service_that_answers_is(
        self, answers: bool, status: int
    ) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            rig.conversation.ready_answer = answers

            response = await http.get("/readyz")

            assert response.status_code == status

    async def test_the_documentation_is_there_for_developers_and_only_them(self) -> None:
        async with Rig() as rig:
            with_docs = create_app(
                DevAuthenticator(TEST, tenant_id=TENANT, user_id=USER),
                sessions=rig.sessions,
                registry=rig.registry,
                conversation=rig.conversation,
                docs=True,
            )

            assert with_docs.docs_url == "/docs"
            assert app_for(rig).docs_url is None


class TestWhoIsConnected:
    async def test_nobody_is_connected_until_somebody_is(self) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            assert (await http.get("/v1/devices")).json() == {"devices": []}

    async def test_a_device_that_is_connected_is_listed_with_what_it_can_do(self) -> None:
        screen = make_device(
            "Living room display",
            "display-01",
            "display-credential",
            capabilities=[Capability.AUDIO_IN, Capability.AUDIO_OUT, Capability.DISPLAY],
            display=DisplayInfo(width=800, height=480, touch=True),
        )
        async with Rig(make_device(), screen) as rig, client_for(app_for(rig)) as http:
            await rig.connect(rig.devices[1])
            await rig.connect(rig.devices[0])

            found = (await http.get("/v1/devices")).json()["devices"]

            assert [d["name"] for d in found] == ["Kitchen speaker", "Living room display"]
            assert found[1]["capabilities"] == ["audio_in", "audio_out", "display"]
            assert found[1]["display"] == {"width": 800, "height": 480, "touch": True}
            assert found[0]["hardware_id"] == "kitchen-01"
            assert found[0]["connected_at"].endswith(("Z", "+00:00"))

    async def test_another_tenant_sees_none_of_them(self) -> None:
        async with Rig() as rig:
            await rig.connect()
            other = client_for(app_for(rig, tenant=DEV_TENANT_ID))
            async with other as http:
                assert (await http.get("/v1/devices")).json() == {"devices": []}


class TestHavingADeviceDoSomething:
    async def send(self, http: httpx.AsyncClient, device: Any, command: Any) -> httpx.Response:
        return await http.post(
            f"/v1/devices/{device}/commands",
            content=CommandRequest(command=command).model_dump_json(),
            headers={"content-type": "application/json"},
        )

    async def test_the_answer_is_what_the_device_says_became_of_it(self) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            connected = await rig.connect()
            device_id = rig.devices[0][0].id

            asked = asyncio.create_task(self.send(http, device_id, SetVolume(level=30)))
            [sent] = await connected.wire.wait_for("command")
            connected.wire.say(Ack(id=sent.id, result=CommandResult.ok()))
            response = await asked

            assert (response.status_code, response.json()["status"]) == (200, "ok")
            assert sent.command == SetVolume(level=30)

    async def test_a_device_that_is_registered_and_not_connected_is_offline_not_missing(
        self,
    ) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            response = await self.send(http, rig.devices[0][0].id, SetVolume(level=30))

            assert (response.status_code, response.json()["status"]) == (200, "offline")

    async def test_what_the_device_is_not_registered_for_is_unsupported_and_is_not_sent(
        self,
    ) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            connected = await rig.connect()

            response = await self.send(http, rig.devices[0][0].id, SetLed(pattern="solid"))  # type: ignore[arg-type]

            assert (response.status_code, response.json()["status"]) == (200, "unsupported")
            assert connected.wire.frames("command") == []

    async def test_a_device_that_does_not_answer_in_time_is_reported_so(self) -> None:
        settings = quick_settings(command_timeout_s=0.05)
        async with Rig(settings=settings) as rig, client_for(app_for(rig)) as http:
            await rig.connect()

            response = await self.send(http, rig.devices[0][0].id, SetVolume(level=30))

            assert response.json()["status"] == CommandStatus.TIMEOUT.value

    @pytest.mark.parametrize("device", ["unknown", "other-tenant"])
    async def test_a_device_that_is_not_there_or_not_the_tenants_is_not_found(
        self, device: str
    ) -> None:
        async with Rig() as rig:
            target = uuid4() if device == "unknown" else rig.devices[0][0].id
            tenant = TENANT if device == "unknown" else DEV_TENANT_ID
            async with client_for(app_for(rig, tenant=tenant)) as http:
                response = await self.send(http, target, SetVolume(level=30))

                assert response.status_code == 404
                assert response.json()["error"]["code"] == "device_not_found"

    @pytest.mark.parametrize(
        "body",
        [
            "{}",
            '{"command": {"name": "self_destruct"}}',
            '{"command": {"name": "set_volume", "level": 900}}',
            "not json",
        ],
    )
    async def test_what_is_not_a_command_of_the_vocabulary_is_refused(self, body: str) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            response = await http.post(
                f"/v1/devices/{rig.devices[0][0].id}/commands",
                content=body,
                headers={"content-type": "application/json"},
            )

            assert response.status_code == 422
            assert body not in response.text

    async def test_a_body_that_is_a_book_is_refused(self) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            response = await http.post(
                f"/v1/devices/{rig.devices[0][0].id}/commands",
                content=b"x" * (MAX_FRAME_BYTES + 1024),
                headers={"content-type": "application/json"},
            )

            assert response.status_code == 413

    async def test_a_command_that_carries_a_clip_is_taken_as_big_as_a_frame_is(self) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            connected = await rig.connect(features=(Feature.PLAY_AUDIO,))
            clip = wav_of(b"" * 300_000, 22_050)  # 600 KB of samples
            command = PlayAudio(text="Dinner is ready.", audio=clip)
            assert len(CommandRequest(command=command).model_dump_json()) > 64 * 1024

            asked = asyncio.create_task(self.send(http, rig.devices[0][0].id, command))
            [sent] = await connected.wire.wait_for("command")
            connected.wire.say(Ack(id=sent.id, result=CommandResult.ok()))
            response = await asked

            assert (response.status_code, response.json()["status"]) == (200, "ok")
            assert isinstance(sent.command, PlayAudio)
            assert sent.command.audio.data == clip.data

    async def test_the_device_is_asked_for_as_the_tenant_knows_it(self) -> None:
        async with Rig() as rig, client_for(app_for(rig)) as http:
            response = await http.post(
                f"/v1/devices/{rig.devices[0][0].id}/commands",
                content=CommandRequest(command=SetVolume(level=1)).model_dump_json(),
                headers={"content-type": "application/json", DEVICE_HEADER: str(uuid4())},
            )

            assert response.status_code == 200  # who the caller says it is a device is beside it
