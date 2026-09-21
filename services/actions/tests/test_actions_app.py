"""The service put together from its settings: what it refuses to start without, and, against a
real database and a gateway that answers, that it comes up, installs and enables a skill, reads a
message, asks for a confirmation that a later message answers, and keeps the trail of it."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import AnyHttpUrl

from factotum.actions.app import build_app
from factotum.actions.rig import HOME_TEXT, plan_of, principal_of_device
from factotum.actions.settings import ActionsSettings
from factotum.actions.testing import DIMENSIONS, embed_words
from factotum.common.config import Environment
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, dev_headers
from factotum.contracts.errors import AuthError
from factotum.db.settings import DatabaseSettings
from factotum.devices.home import parse_home

GATEWAY = "http://gateway.test"

WEATHER = """\
---
name: weather
description: Say what the weather is like somewhere.
examples:
  - what is the weather in Rome
  - will it rain tomorrow
target: none
parameters:
  place: {type: string}
required: [place]
effect: read_only
---
"""

GARAGE = """\
---
name: garage-door
description: Open or close the garage door.
examples:
  - open the garage door
  - close the garage
target: external
parameters:
  action: {type: string, enum: [open, close]}
required: [action]
effect: irreversible
confirm: ask
---
"""


def settings_for(database: DatabaseSettings, **values: object) -> ActionsSettings:
    return ActionsSettings(
        _env_file=None,
        environment=Environment.TEST,
        postgres_host=database.postgres_host,
        postgres_port=database.postgres_port,
        postgres_user=database.postgres_user,
        postgres_password=database.postgres_password,
        postgres_db=database.postgres_db,
        actions_gateway_url=AnyHttpUrl(GATEWAY),
        **values,  # type: ignore[arg-type]
    )


class TestStartingUp:
    def test_it_does_not_start_in_production(self) -> None:
        with pytest.raises(AuthError, match="disabled"):
            build_app(ActionsSettings(_env_file=None, environment=Environment.PRODUCTION))

    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_it_starts_in_development_and_in_tests(self, environment: Environment) -> None:
        app = build_app(ActionsSettings(_env_file=None, environment=environment))

        paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
        assert {"/v1/act", "/v1/skills", "/v1/audit", "/healthz", "/readyz"} <= paths

    def test_the_documentation_is_there_for_developers(self) -> None:
        app = build_app(ActionsSettings(_env_file=None, environment=Environment.DEVELOPMENT))

        assert app.docs_url == "/docs"

    def test_a_file_of_devices_that_is_named_and_is_not_there_stops_it(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            build_app(
                ActionsSettings(
                    _env_file=None,
                    environment=Environment.TEST,
                    actions_devices_file=tmp_path / "nowhere.yaml",
                )
            )

    def test_a_file_that_is_not_one_of_devices_stops_it_and_says_which(
        self, tmp_path: Path
    ) -> None:
        wrong = tmp_path / "devices.yaml"
        wrong.write_text("nothing: here\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match=r"devices\.yaml"):
            build_app(
                ActionsSettings(
                    _env_file=None, environment=Environment.TEST, actions_devices_file=wrong
                )
            )

    async def test_without_a_database_it_does_not_come_up(self) -> None:
        app = build_app(
            ActionsSettings(
                _env_file=None,
                environment=Environment.TEST,
                postgres_port=1,
                postgres_password="x",  # type: ignore[arg-type]
            )
        )

        with pytest.raises(OSError):  # noqa: PT011 - what is refused is the connection
            async with app.router.lifespan_context(app):
                pass  # pragma: no cover - the database is not there


class Model:
    """The gateway, as far as this service can tell: it reads messages and it embeds."""

    def __init__(self) -> None:
        self.plans: list[dict[str, Any]] = []
        self.reads: list[dict[str, str]] = []
        self.asked: list[dict[str, Any]] = []

    def chat(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        self.asked.append(body)
        # The plan of a message has a schema with a `skill`; the reading of a yes or a no, a `kind`.
        properties = body["response_schema"]["properties"]
        said = self.plans.pop(0) if "skill" in properties else self.reads.pop(0)
        return httpx.Response(
            200, json={"text": json.dumps(said), "parsed": said, "finish_reason": "stop"}
        )

    @staticmethod
    def embeddings(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["inputs"]
        return httpx.Response(
            200,
            json={
                "vectors": [list(embed_words(text)) for text in texts],
                "dimensions": DIMENSIONS,
                "space_id": "space-a",
            },
        )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
class TestAgainstARealDatabase:
    @respx.mock
    async def test_a_skill_is_installed_enabled_used_and_audited(
        self, database_settings: DatabaseSettings, tmp_path: Path
    ) -> None:
        model = Model()
        respx.post(f"{GATEWAY}/v1/chat").mock(side_effect=model.chat)
        respx.post(f"{GATEWAY}/v1/embeddings").mock(side_effect=model.embeddings)
        home = tmp_path / "devices.yaml"
        home.write_text(HOME_TEXT, encoding="utf-8", newline="\n")
        parsed = parse_home(HOME_TEXT, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
        display = next(r for r, _ in parsed.system if r.name == "Living room display")
        speaker = next(r for r, _ in parsed.system if r.name == "Kitchen speaker")
        display_headers = dev_headers(principal_of_device(display))
        speaker_headers = dev_headers(principal_of_device(speaker))
        app = build_app(settings_for(database_settings, actions_devices_file=home))

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://actions"
            ) as http,
        ):
            assert (await http.get("/readyz")).status_code == 200

            # Installed, a skill does not run; enabled by an administrator, it does.
            weather = (await http.post("/v1/skills", json={"source": WEATHER})).json()
            garage = (await http.post("/v1/skills", json={"source": GARAGE})).json()
            asked = "what is the weather in Rome"
            before = await http.post("/v1/act", json={"message": asked}, headers=speaker_headers)
            assert before.json()["status"] == "no_action"
            assert model.asked == []  # nothing runs, so nothing was offered to the model

            for skill in (weather, garage):
                enabled = await http.post(f"/v1/skills/{skill['id']}/enable")
                assert enabled.json()["enabled_version"] == 1

            # A message that asks for a skill that needs nothing more: it is done (as a dry run).
            model.plans.append(plan_of("weather", parameters={"place": "Rome"}))
            done = await http.post("/v1/act", json={"message": asked}, headers=speaker_headers)
            assert done.json()["status"] == "done"
            assert "Dry run" in done.json()["reply"]

            # A skill that asks first: what was understood is kept until a yes or a no.
            model.plans.append(
                plan_of(
                    "garage-door",
                    device="Garage door",
                    parameters={"action": "open"},
                    question="Do you really want me to open the garage door?",
                )
            )
            opened = "open the garage door"
            asking = await http.post("/v1/act", json={"message": opened}, headers=display_headers)
            assert asking.json()["status"] == "needs_confirmation"
            assert asking.json()["reply"] == "Do you really want me to open the garage door?"

            # The next message on the same device is the answer, and a plain yes is read by its
            # words, so the model is not asked.
            asked_before = len(model.asked)
            yes = await http.post("/v1/act", json={"message": "yes"}, headers=display_headers)
            assert yes.json()["status"] == "done"
            assert len(model.asked) == asked_before

            # And it is not asked twice: a yes that comes again answers nothing.
            again = await http.post("/v1/act", json={"message": "yes"}, headers=display_headers)
            assert again.json()["status"] == "no_action"

            # Everything that was decided is in the trail, the latest first.
            trail = (await http.get("/v1/audit")).json()["records"]
            assert [(r["skill_name"], r["decision"], r["confirmed"]) for r in trail] == [
                ("garage-door", "require_confirmation", True),
                ("garage-door", "require_confirmation", None),
                ("weather", "allow", None),
            ]

            # Deleting a skill takes its versions and vectors with it, and leaves the trail.
            assert (await http.delete(f"/v1/skills/{weather['id']}")).status_code == 204
            assert (await http.get(f"/v1/skills/{weather['id']}")).status_code == 404
            assert len((await http.get("/v1/audit")).json()["records"]) == len(trail)
