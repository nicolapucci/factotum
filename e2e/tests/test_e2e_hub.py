"""The assistant as it runs in production: with the hub.

Nothing here reads a header that says whom a call is for. A secret is shared, every service checks
the signed token it is shown and mints a short one of its own to call the next, and what was a file
in development is the hub's: the devices, each with the credential it was given when it was paired,
the skills bound to the external ones, the models bound to a role, and the secrets of the home.

The home is the example one, paired in a hub of this process by its administrator (see
``factotum.e2e.wiring``). The journeys are the ones of the other modules, run against it, and what
only the hub has to say: a credential it did not issue, and a call that comes with no token.
"""

import pytest

from factotum.common.devauth import SUBJECT_HEADER
from factotum.common.tokens import encode_token
from factotum.contracts.edge import TurnStatus
from factotum.contracts.identity import Principal
from factotum.contracts.knowledge import CollectionOwner
from factotum.e2e.devices import connected
from factotum.e2e.house import House, running
from factotum.e2e.models import NOT_FOUND, OFFER, ROUTER, SPOKEN_ANSWER
from factotum.e2e.wiring import HA_TOKEN, TENANT, Auth
from factotum.terminal.client import Refused

HOME_ASSISTANT = "http://homeassistant.local:8123"
QUESTION = "How do I reset the router?"
SAFE = "The code of the safe is 4711."


def reached(house: House) -> list[str]:
    """What Home Assistant was asked, as the call and the entity, in order."""
    return [
        f"{seen.target.removeprefix(HOME_ASSISTANT)} {seen.json()['entity_id']}"
        for seen in house.home_assistant.seen
    ]


class TestADeviceThatTheHubPaired:
    async def test_a_spoken_question_is_answered_aloud_from_the_documents(self) -> None:
        async with running(auth=Auth.HUB) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.says_aloud(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                assert speaker.spoken() == SPOKEN_ANSWER

    async def test_a_lamp_is_switched_with_the_secret_the_hub_keeps(self) -> None:
        async with running(auth=Auth.HUB) as house, connected(house, "speaker") as speaker:
            ended = await speaker.says_aloud("Turn on the lamp.")

            assert ended.status is TurnStatus.FINISHED, ended
            # The entity is the one the hub bound the skill to, and the token is the secret the hub
            # holds: the script was given nothing else.
            [call] = house.home_assistant.seen
            assert call.headers["authorization"] == f"Bearer {HA_TOKEN}"
            assert reached(house) == ["/api/services/light/turn_on light.living_room"]
            [job] = house.runner.jobs
            assert (job.label, job.hosts, job.env) == (
                "lamp",
                ("homeassistant.local",),
                {"HA_TOKEN": HA_TOKEN},
            )
            assert speaker.spoken() == ["The lamp is on."]

    async def test_the_garage_door_follows_the_policy_the_hub_has_for_each_device(self) -> None:
        async with (
            running(auth=Auth.HUB) as house,
            connected(house, "speaker") as speaker,
            connected(house, "smart-display") as display,
        ):
            await speaker.says_aloud("Open the garage door.")
            refused = list(house.home_assistant.seen)
            await display.says_aloud("Open the garage door.")
            asked = list(house.home_assistant.seen)
            await display.says_aloud("Yes.")

            assert speaker.spoken() == ["This device is not allowed to do that."]
            assert (refused, asked) == ([], [])
            assert display.spoken()[0] == "Do you really want me to open the garage door?"
            assert reached(house) == ["/api/services/cover/open_cover cover.garage"]

    async def test_a_lamp_that_is_a_persons_own_is_theirs_and_nobody_elses(self) -> None:
        async with (
            running(auth=Auth.HUB) as house,
            connected(house, "phone") as phone,
            connected(house, "speaker") as speaker,
        ):
            await phone.says_aloud("Turn on my lamp.")
            await speaker.says_aloud("Turn on my lamp.")

            assert reached(house) == ["/api/services/light/turn_on light.bedroom"]
            assert speaker.spoken() == ["Which one do you mean: Living room lamp?"]
            assert "Bedroom" not in str(speaker.shown.shown)

    async def test_a_private_document_is_read_out_by_its_owners_phone_and_never_by_the_speaker(
        self,
    ) -> None:
        async with running(auth=Auth.HUB) as house:
            await house.library.add("Private", "The safe", SAFE, owner=CollectionOwner.USER)
            async with connected(house, "speaker") as speaker, connected(house, "phone") as phone:
                await phone.says_aloud("What is the code of the safe?")
                await speaker.says_aloud("What is the code of the safe?")

                assert phone.spoken()[0] == "From your documents: The code of the safe is 4711."
                assert speaker.spoken() == [NOT_FOUND, OFFER]
                assert "4711" not in str(speaker.shown.shown)


class TestWhoIsTrusted:
    async def test_a_device_with_the_placeholder_of_the_example_file_is_refused(self) -> None:
        async with running(auth=Auth.HUB) as house:
            with pytest.raises(Refused) as refused:
                async with connected(house, "speaker", credential="kitchen-dev-token"):
                    pass

            assert refused.value.code == "auth_failed"

    async def test_a_device_with_the_credential_of_another_is_refused(self) -> None:
        async with running(auth=Auth.HUB) as house:
            with pytest.raises(Refused) as refused:
                async with connected(house, "speaker", credential=house.tokens["phone-01"]):
                    pass

            assert refused.value.code == "auth_failed"

    @pytest.mark.parametrize(
        ("service", "path"),
        [
            ("model-gateway", "/v1/profiles/chat.main"),
            ("knowledge", "/v1/collections"),
            ("conversation", "/v1/conversations"),
            ("actions", "/v1/skills"),
            ("hub", "/v1/devices"),
        ],
    )
    async def test_every_service_checks_the_token_it_is_shown(
        self, service: str, path: str
    ) -> None:
        async with running(auth=Auth.HUB) as house:
            interface = house.services[service]
            stranger = {
                "authorization": "Bearer "
                + encode_token(
                    Principal.of_tenant(TENANT), secret=b"a-secret-that-is-not-the-hubs", ttl_s=60
                )
            }

            statuses = {
                "the administrator's token": (await interface.get(path, headers=house.admin)),
                "a token that another secret signed": (await interface.get(path, headers=stranger)),
                "the headers of development": (
                    await interface.get(path, headers={SUBJECT_HEADER: "user"})
                ),
                "nothing": (await interface.get(path)),
            }

            assert {what: reply.status_code for what, reply in statuses.items()} == {
                "the administrator's token": 200,
                "a token that another secret signed": 401,
                "the headers of development": 401,
                "nothing": 401,
            }
