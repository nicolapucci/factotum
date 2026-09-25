"""The service put together from its settings: what it refuses to start without, and, against a
real database and a gateway that answers, that it comes up, installs and enables a skill, reads a
message, asks for a confirmation that a later message answers, and keeps the trail of it."""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import AnyHttpUrl

from factotum.actions.app import build_app
from factotum.actions.instructions.testing import plan_dict, step_command, step_http
from factotum.actions.rig import HOME_TEXT, plan_of, principal_of_device
from factotum.actions.settings import ActionsSettings
from factotum.actions.testing import DIMENSIONS, embed_words
from factotum.common.config import Environment
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, dev_headers
from factotum.contracts.errors import AuthError
from factotum.contracts.runner import JobResult
from factotum.db.settings import DatabaseSettings
from factotum.devices.home import parse_home

GATEWAY = "http://gateway.test"
RUNNER = "http://runner.test"
EDGE = "http://edge.test"
EXAMPLES = Path(__file__).resolve().parents[3] / "skills" / "examples"
SECRETS = """\
tenant:
  HA_TOKEN: the-token-of-the-home
"""

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


MOVIE_SCENE = """\
---
name: movie-scene
description: Dim the lights of the living room for watching a movie.
examples:
  - set the movie scene
  - it is movie time
target: none
permissions:
  network: [homeassistant.local]
  secrets: [HA_TOKEN]
effect: reversible
---
Switch the ceiling light off: POST `http://homeassistant.local:8123/api/services/light/turn_off`
with `{"entity_id": "light.living_room_ceiling"}` and the header `Authorization: Bearer TOKEN`.
Say that the movie scene is set.
"""

ANNOUNCE = """\
---
name: announce
description: Say something out loud on a device, such as that dinner is ready.
examples:
  - tell everyone that dinner is ready
  - announce that the guests have arrived
target: system
parameters:
  message: {type: string}
required: [message]
effect: reversible
---
Give the device a speak command with the message as its text.
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

    def test_a_file_of_secrets_that_is_named_and_is_not_there_stops_it(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            build_app(
                ActionsSettings(
                    _env_file=None,
                    environment=Environment.TEST,
                    actions_secrets_file=tmp_path / "nowhere.yaml",
                )
            )

    def test_a_file_that_is_not_one_of_secrets_stops_it_and_says_which_and_not_what(
        self, tmp_path: Path
    ) -> None:
        wrong = tmp_path / "secrets.dev.yaml"
        wrong.write_text("tenant:\n  HA_TOKEN: 424242\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match=r"secrets\.dev\.yaml") as raised:
            build_app(
                ActionsSettings(
                    _env_file=None, environment=Environment.TEST, actions_secrets_file=wrong
                )
            )

        assert "424242" not in str(raised.value)

    def test_it_says_when_it_starts_what_is_not_there(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="factotum.actions")

        build_app(ActionsSettings(_env_file=None, environment=Environment.TEST))

        said = caplog.text
        assert "no runner is set" in said
        assert "no edge is set" in said
        assert "no file of secrets is set" in said

    def test_it_does_not_complain_of_what_is_there(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        secrets = tmp_path / "secrets.dev.yaml"
        secrets.write_text(SECRETS, encoding="utf-8", newline="\n")
        caplog.set_level(logging.WARNING, logger="factotum.actions")

        build_app(
            ActionsSettings(
                _env_file=None,
                environment=Environment.TEST,
                actions_runner_url=AnyHttpUrl(RUNNER),
                actions_edge_url=AnyHttpUrl(EDGE),
                actions_secrets_file=secrets,
            )
        )

        assert "no runner" not in caplog.text
        assert "no edge" not in caplog.text
        assert "no file of secrets" not in caplog.text
        assert "the-token-of-the-home" not in caplog.text

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
        self.written: list[dict[str, Any]] = []
        self.reads: list[dict[str, str]] = []
        self.asked: list[dict[str, Any]] = []

    def chat(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        self.asked.append(body)
        # What is asked is known by the shape of the answer: the plan of a message has a `skill`,
        # the plan of a skill that has no script has `steps`, the reading of a yes or a no a `kind`.
        properties = body["response_schema"]["properties"]
        if "skill" in properties:
            said = self.plans.pop(0)
        elif "steps" in properties:
            said = self.written.pop(0)
        else:
            said = self.reads.pop(0)
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

    @respx.mock
    async def test_a_skill_with_a_script_is_run_by_the_runner_and_its_command_reaches_a_device(
        self, database_settings: DatabaseSettings, tmp_path: Path
    ) -> None:
        model = Model()
        respx.post(f"{GATEWAY}/v1/chat").mock(side_effect=model.chat)
        respx.post(f"{GATEWAY}/v1/embeddings").mock(side_effect=model.embeddings)
        printed = {
            "message": "Volume set to 30.",
            "commands": [{"name": "set_volume", "level": 30}],
        }
        job = JobResult(exit_code=0, stdout=json.dumps(printed))
        runner = respx.post(f"{RUNNER}/v1/jobs").mock(
            return_value=httpx.Response(200, json=json.loads(job.model_dump_json()))
        )
        edge = respx.post(url__regex=rf"{EDGE}/v1/devices/.+/commands").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        home = tmp_path / "devices.yaml"
        home.write_text(HOME_TEXT, encoding="utf-8", newline="\n")
        secrets = tmp_path / "secrets.dev.yaml"
        secrets.write_text(SECRETS, encoding="utf-8", newline="\n")
        parsed = parse_home(HOME_TEXT, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
        speaker = next(r for r, _ in parsed.system if r.name == "Kitchen speaker")
        app = build_app(
            settings_for(
                database_settings,
                actions_devices_file=home,
                actions_secrets_file=secrets,
                actions_skills_dir=EXAMPLES,
                actions_enable_skills_dir=True,
                actions_runner_url=AnyHttpUrl(RUNNER),
                actions_edge_url=AnyHttpUrl(EDGE),
            )
        )

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://actions"
            ) as http,
        ):
            reloaded = await http.post("/v1/skills/reload")
            assert "volume@1" in reloaded.json()["enabled"]
            model.plans.append(plan_of("volume", parameters={"level": 30}))

            done = await http.post(
                "/v1/act",
                json={"message": "set the volume to 30"},
                headers=dev_headers(principal_of_device(speaker)),
            )

        assert done.json()["status"] == "done"
        assert done.json()["reply"] == "Volume set to 30."
        assert runner.call_count == 1
        sent = json.loads(runner.calls[0].request.content)
        assert sent["label"] == "volume"
        assert sent["entrypoint"] == "scripts/run.py"
        assert [file["path"] for file in sent["files"]] == ["scripts/run.py"]
        assert (sent["env"], sent["hosts"]) == ({}, [])
        assert edge.call_count == 1
        command = json.loads(edge.calls[0].request.content)
        assert command == {"command": {"name": "set_volume", "level": 30}}
        assert edge.calls[0].request.url.path == f"/v1/devices/{speaker.id}/commands"

    @respx.mock
    async def test_a_skill_that_is_instructions_is_given_a_plan_and_the_runner_runs_the_program(
        self, database_settings: DatabaseSettings, tmp_path: Path
    ) -> None:
        model = Model()
        respx.post(f"{GATEWAY}/v1/chat").mock(side_effect=model.chat)
        respx.post(f"{GATEWAY}/v1/embeddings").mock(side_effect=model.embeddings)
        printed = {"message": "The movie scene is set.", "commands": []}
        runner = respx.post(f"{RUNNER}/v1/jobs").mock(
            return_value=httpx.Response(
                200,
                json=json.loads(
                    JobResult(exit_code=0, stdout=json.dumps(printed)).model_dump_json()
                ),
            )
        )
        home = tmp_path / "devices.yaml"
        home.write_text(HOME_TEXT, encoding="utf-8", newline="\n")
        secrets = tmp_path / "secrets.dev.yaml"
        secrets.write_text(SECRETS, encoding="utf-8", newline="\n")
        parsed = parse_home(HOME_TEXT, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
        speaker = next(r for r, _ in parsed.system if r.name == "Kitchen speaker")
        app = build_app(
            settings_for(
                database_settings,
                actions_devices_file=home,
                actions_secrets_file=secrets,
                actions_runner_url=AnyHttpUrl(RUNNER),
                actions_edge_url=AnyHttpUrl(EDGE),
            )
        )
        written = plan_dict(
            step_http(
                "ceiling",
                "POST",
                "http://homeassistant.local:8123/api/services/light/turn_off",
                headers={"Authorization": "Bearer ${secret.HA_TOKEN}"},
                body={"entity_id": "light.living_room_ceiling"},
            ),
            say="The movie scene is set.",
        )

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://actions"
            ) as http,
        ):
            installed = (await http.post("/v1/skills", json={"source": MOVIE_SCENE})).json()
            assert installed["mode"] == "instructions"
            await http.post(f"/v1/skills/{installed['id']}/enable")
            model.plans.append(plan_of("movie-scene"))
            model.written.append(written)

            done = await http.post(
                "/v1/act",
                json={"message": "set the movie scene"},
                headers=dev_headers(principal_of_device(speaker)),
            )
            trail = (await http.get("/v1/audit")).json()["records"]

        assert (done.json()["status"], done.json()["reply"]) == (
            "done",
            "The movie scene is set.",
        )
        # The model that wrote the plan was given what the skill says, and no secret.
        [_, writing] = model.asked
        assert "light/turn_off" in writing["messages"][-1]["content"]
        assert "the-token-of-the-home" not in json.dumps(model.asked)
        # What was run is the program that runs plans and the plan, with the secret only in the
        # environment of the run: the plan holds its name.
        assert runner.call_count == 1
        sent = json.loads(runner.calls[0].request.content)
        assert (sent["label"], sent["entrypoint"]) == ("movie-scene", "executor.py")
        assert [file["path"] for file in sent["files"]] == ["executor.py"]
        assert (sent["env"], sent["hosts"]) == (
            {"HA_TOKEN": "the-token-of-the-home"},
            ["homeassistant.local"],
        )
        given = json.loads(sent["stdin"])
        assert given["plan"]["steps"][0]["headers"] == {
            "Authorization": "Bearer ${secret.HA_TOKEN}"
        }
        assert given["secrets"] == ["HA_TOKEN"]
        assert "the-token-of-the-home" not in sent["stdin"]
        # And the trail says what was done.
        assert trail[0]["skill_name"] == "movie-scene"
        assert trail[0]["detail"].startswith("plan: POST homeassistant.local")

    @respx.mock
    async def test_what_a_plan_has_a_device_say_reaches_the_device_through_the_edge(
        self, database_settings: DatabaseSettings, tmp_path: Path
    ) -> None:
        model = Model()
        respx.post(f"{GATEWAY}/v1/chat").mock(side_effect=model.chat)
        respx.post(f"{GATEWAY}/v1/embeddings").mock(side_effect=model.embeddings)
        printed = {"commands": [{"name": "speak", "text": "dinner is ready"}]}
        runner = respx.post(f"{RUNNER}/v1/jobs").mock(
            return_value=httpx.Response(
                200,
                json=json.loads(
                    JobResult(exit_code=0, stdout=json.dumps(printed)).model_dump_json()
                ),
            )
        )
        edge = respx.post(url__regex=rf"{EDGE}/v1/devices/.+/commands").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        home = tmp_path / "devices.yaml"
        home.write_text(HOME_TEXT, encoding="utf-8", newline="\n")
        parsed = parse_home(HOME_TEXT, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
        speaker = next(r for r, _ in parsed.system if r.name == "Kitchen speaker")
        app = build_app(
            settings_for(
                database_settings,
                actions_devices_file=home,
                actions_runner_url=AnyHttpUrl(RUNNER),
                actions_edge_url=AnyHttpUrl(EDGE),
            )
        )

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://actions"
            ) as http,
        ):
            installed = (await http.post("/v1/skills", json={"source": ANNOUNCE})).json()
            await http.post(f"/v1/skills/{installed['id']}/enable")
            model.plans.append(plan_of("announce", parameters={"message": "dinner is ready"}))
            model.written.append(plan_dict(step_command("say", "speak", text="${value.message}")))

            done = await http.post(
                "/v1/act",
                json={"message": "tell everyone that dinner is ready"},
                headers=dev_headers(principal_of_device(speaker)),
            )

        assert done.json()["status"] == "done"
        sent = json.loads(runner.calls[0].request.content)
        assert json.loads(sent["stdin"])["params"] == {"message": "dinner is ready"}
        assert (sent["env"], sent["hosts"]) == ({}, [])
        assert edge.call_count == 1
        command = json.loads(edge.calls[0].request.content)["command"]
        assert (command["name"], command["text"]) == ("speak", "dinner is ready")
        assert edge.calls[0].request.url.path == f"/v1/devices/{speaker.id}/commands"
