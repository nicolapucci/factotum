from typing import Any

import pytest

from factotum.actions.directory import BoundDevice, FileDeviceDirectory
from factotum.actions.matching import Candidate
from factotum.actions.planner import Plan
from factotum.actions.testing import GARAGE, LAMP, VOLUME, WEATHER, stored_skill
from factotum.actions.understanding import (
    MAX_SENTENCE_CHARS,
    NeedsMore,
    NothingToDo,
    Understood,
    clean_sentence,
    understand,
)
from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.actions import ActCode
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.identity import Principal, Role, Subject
from factotum.contracts.testing import make_tenant_id, make_user_id
from factotum.devices.home import parse_home

TENANT = make_tenant_id()
USER = make_user_id()
TEST = ServiceSettings(_env_file=None, environment=Environment.TEST)

HOME = """
devices:
  - name: Kitchen speaker
    hardware_id: kitchen-01
    token: kitchen-credential
    owner: tenant
    capabilities: [audio_in, audio_out]
    aliases: [the kitchen]
  - name: Study player
    hardware_id: study-01
    token: study-credential
    owner: tenant
    capabilities: [audio_out, media_player]
  - name: My phone
    hardware_id: phone-01
    token: phone-credential
    owner: user
    capabilities: [audio_in, audio_out]
  - kind: external
    name: Living room lamp
    aliases: [lamp, the lamp]
  - kind: external
    name: Bedroom lamp
    owner: user
    aliases: [My Lamp]
  - kind: external
    name: Garage door
    sensitivity: irreversible
bindings:
  - skill: lamp
    device: Living room lamp
    params: {entity_id: light.living_room, host: homeassistant.local}
  - skill: lamp
    device: Bedroom lamp
    params: {entity_id: light.bedroom, host: homeassistant.local}
  - skill: garage-door
    device: Garage door
    params: {entity_id: cover.garage}
"""

CANDIDATES = [
    Candidate(stored_skill(manifest, TENANT)) for manifest in (LAMP, GARAGE, VOLUME, WEATHER)
]


def plan(skill: str | None = "lamp", **fields: Any) -> Plan:
    given: dict[str, Any] = {"reason": "as told", "skill": skill, "language": "en", **fields}
    return Plan.model_validate(given)


def shared() -> Principal:
    return Principal(tenant_id=TENANT, acts_as=Subject.tenant(TENANT))


def person() -> Principal:
    return Principal(tenant_id=TENANT, acts_as=Subject.user(USER), roles=frozenset({Role.MEMBER}))


async def understood(
    the_plan: Plan,
    who: Principal | None = None,
    *,
    here: str | None = "Kitchen speaker",
    text: str = HOME,
    candidates: list[Candidate] | None = None,
) -> Any:
    home = parse_home(text, tenant_id=TENANT, user_id=USER, source="home")
    directory = FileDeviceDirectory(TEST, home)
    caller = who or shared()
    systems = await directory.system_devices(caller)
    found_here: SystemDeviceRecord | None = next((d for d in systems if d.name == here), None)
    bound: dict[str, list[BoundDevice]] = {
        name: await directory.bound_devices(caller, name) for name in ("lamp", "garage-door")
    }
    return understand(
        the_plan,
        CANDIDATES if candidates is None else candidates,
        here=found_here,
        systems=systems,
        bound=bound,
    )


class TestWhatIsNotAnAction:
    @pytest.mark.parametrize("skill", [None, "", "   "])
    async def test_a_plan_with_no_skill_is_nothing_to_do(self, skill: str | None) -> None:
        assert await understood(plan(skill)) == NothingToDo()

    async def test_a_skill_that_was_not_offered_is_nothing_to_do_however_sure_the_model_was(
        self,
    ) -> None:
        assert await understood(plan("format-the-disk", parameters={"disk": "c"})) == NothingToDo()

    async def test_a_skill_that_was_offered_but_is_not_running_is_nothing_to_do(self) -> None:
        idle = [Candidate(stored_skill(LAMP, TENANT, enabled=False))]

        assert await understood(plan(parameters={"state": "on"}), candidates=idle) == NothingToDo()

    async def test_the_skill_is_found_in_any_case_and_with_spaces_around(self) -> None:
        found = await understood(plan(" Lamp ", parameters={"state": "on"}))

        assert isinstance(found, Understood)
        assert found.candidate.skill.name == "lamp"


class TestASkillThatActsOnNothing:
    async def test_a_skill_with_no_device_is_understood_with_its_values(self) -> None:
        found = await understood(plan("weather", parameters={"place": "Rome"}))

        assert isinstance(found, Understood)
        assert (found.values, found.device_params, found.target) == ({"place": "Rome"}, {}, None)

    async def test_a_device_the_model_named_is_ignored_when_the_skill_acts_on_none(self) -> None:
        found = await understood(
            plan("weather", device="Garage door", parameters={"place": "Rome"})
        )

        assert isinstance(found, Understood)
        assert found.target is None


class TestAnExternalDevice:
    async def test_the_only_device_a_skill_is_set_up_for_is_the_one_meant(self) -> None:
        found = await understood(plan(parameters={"state": "on"}))

        assert isinstance(found, Understood)
        assert found.target is not None
        assert found.target.name == "Living room lamp"
        assert found.device_params == {
            "entity_id": "light.living_room",
            "host": "homeassistant.local",
        }

    @pytest.mark.parametrize("said", ["Living room lamp", "lamp", "THE LAMP", " the  lamp "])
    async def test_a_device_is_meant_by_its_name_or_an_alias_in_any_case(self, said: str) -> None:
        found = await understood(plan(device=said, parameters={"state": "off"}), person())

        assert isinstance(found, Understood)
        assert found.target is not None
        assert found.target.name == "Living room lamp"

    async def test_a_personal_device_is_meant_for_the_person_it_belongs_to(self) -> None:
        found = await understood(plan(device="my lamp", parameters={"state": "on"}), person())

        assert isinstance(found, Understood)
        assert found.target is not None
        assert found.target.name == "Bedroom lamp"
        assert found.device_params["entity_id"] == "light.bedroom"

    async def test_which_one_is_asked_when_it_is_not_said_and_there_are_several(self) -> None:
        found = await understood(plan(parameters={"state": "on"}), person())

        assert isinstance(found, NeedsMore)
        assert found.code is ActCode.AMBIGUOUS_DEVICE
        assert found.reply == "Which one do you mean: Bedroom lamp, Living room lamp?"
        assert found.skill == "lamp"

    async def test_which_one_is_asked_when_the_name_is_not_one_of_theirs(self) -> None:
        found = await understood(plan(device="the stove", parameters={"state": "on"}), person())

        assert isinstance(found, NeedsMore)
        assert found.code is ActCode.AMBIGUOUS_DEVICE
        assert "Bedroom lamp" in found.reply

    async def test_which_one_is_asked_when_a_name_is_two_devices(self) -> None:
        text = HOME.replace("aliases: [My Lamp]", "aliases: [lamp]")

        found = await understood(
            plan(device="lamp", parameters={"state": "on"}), person(), text=text
        )

        assert isinstance(found, NeedsMore)
        assert found.code is ActCode.AMBIGUOUS_DEVICE

    async def test_a_person_who_cannot_use_a_device_is_not_told_it_is_there(self) -> None:
        # The shared speaker cannot use the bedroom lamp: it is not offered, and not named.
        found = await understood(plan(device="my lamp", parameters={"state": "on"}))

        assert isinstance(found, NeedsMore)
        assert found.code is ActCode.AMBIGUOUS_DEVICE
        assert "Bedroom" not in found.reply

    async def test_a_skill_that_is_set_up_for_no_device_of_the_caller_says_so(self) -> None:
        stranger = Principal(tenant_id=TENANT, acts_as=Subject.tenant(TENANT))
        text = HOME.split("bindings:")[0]

        found = await understood(plan(parameters={"state": "on"}), stranger, text=text)

        assert isinstance(found, NeedsMore)
        assert (found.code, found.reply) == (
            ActCode.NO_DEVICE,
            "There is no device set up for that.",
        )

    async def test_a_device_set_up_with_what_the_skill_does_not_accept_is_not_run_on(self) -> None:
        text = HOME.replace("host: homeassistant.local}\n  - skill: lamp", "}\n  - skill: lamp", 1)

        found = await understood(plan(parameters={"state": "on"}), text=text)

        assert isinstance(found, NeedsMore)
        assert found.code is ActCode.NO_DEVICE


class TestASystemDevice:
    async def test_the_device_that_was_spoken_to_is_the_one_meant(self) -> None:
        found = await understood(plan("volume", parameters={"level": 30}))

        assert isinstance(found, Understood)
        assert found.target is not None
        assert found.target.name == "Kitchen speaker"

    async def test_another_device_can_be_named(self) -> None:
        found = await understood(plan("volume", device="study player", parameters={"level": 30}))

        assert isinstance(found, Understood)
        assert found.target is not None
        assert found.target.name == "Study player"

    async def test_a_device_can_be_named_by_an_alias(self) -> None:
        found = await understood(plan("volume", device="The Kitchen", parameters={"level": 5}))

        assert isinstance(found, Understood)
        assert found.target is not None
        assert found.target.name == "Kitchen speaker"

    async def test_a_name_that_is_nobodys_is_asked_again_with_the_devices_there_are(self) -> None:
        found = await understood(plan("volume", device="the fridge", parameters={"level": 30}))

        assert isinstance(found, NeedsMore)
        assert found.code is ActCode.AMBIGUOUS_DEVICE
        assert found.reply == "Which one do you mean: Kitchen speaker, Study player?"

    async def test_a_personal_device_is_not_offered_to_a_shared_one(self) -> None:
        found = await understood(plan("volume", device="my phone", parameters={"level": 30}))

        assert isinstance(found, NeedsMore)
        assert "phone" not in found.reply

    async def test_with_no_device_that_spoke_and_none_named_it_is_asked_which(self) -> None:
        found = await understood(plan("volume", parameters={"level": 30}), here=None)

        assert isinstance(found, NeedsMore)
        assert found.code is ActCode.NO_DEVICE


class TestTheValues:
    async def test_the_values_are_what_the_skill_declares(self) -> None:
        found = await understood(plan(parameters={"state": "On", "brightness": "40%", "x": 1}))

        assert isinstance(found, Understood)
        assert found.values == {"state": "on", "brightness": 40}

    async def test_a_value_that_is_missing_is_asked_for_in_the_words_of_the_model(self) -> None:
        found = await understood(plan("weather", ask="Which city do you want the weather for?"))

        assert isinstance(found, NeedsMore)
        assert (found.code, found.reply) == (
            ActCode.MISSING_VALUES,
            "Which city do you want the weather for?",
        )

    async def test_or_in_a_catalog_sentence_when_the_model_wrote_none_that_can_be_said(
        self,
    ) -> None:
        for ask in (None, "", "x", "y" * (MAX_SENTENCE_CHARS + 1), "two\nlines\x00"):
            found = await understood(plan("weather", ask=ask))

            assert isinstance(found, NeedsMore)
            assert found.reply == "I still need: place."

    async def test_in_the_language_of_the_person(self) -> None:
        found = await understood(plan("weather", language="it"))

        assert isinstance(found, NeedsMore)
        assert found.reply == "Mi serve ancora: place."

    async def test_a_language_there_are_no_words_for_is_answered_in_english(self) -> None:
        found = await understood(plan("weather", language="xx"))

        assert isinstance(found, NeedsMore)
        assert found.reply == "I still need: place."

    async def test_a_value_that_is_wrong_is_asked_for_too(self) -> None:
        found = await understood(plan(parameters={"state": "on", "brightness": 500}))

        assert isinstance(found, NeedsMore)
        assert found.reply == "I still need: brightness."

    async def test_what_is_missing_comes_before_what_is_wrong(self) -> None:
        found = await understood(plan(parameters={"brightness": 500}))

        assert isinstance(found, NeedsMore)
        assert found.reply == "I still need: state, brightness."

    async def test_the_device_is_asked_before_the_values(self) -> None:
        found = await understood(plan(parameters={}), person())

        assert isinstance(found, NeedsMore)
        assert found.code is ActCode.AMBIGUOUS_DEVICE


class TestTheQuestion:
    async def test_what_the_model_wrote_to_ask_is_kept_as_one_clean_line(self) -> None:
        found = await understood(
            plan(parameters={"state": "on"}, question="  Do you want me\n to turn it on?  ")
        )

        assert isinstance(found, Understood)
        assert found.question == "Do you want me to turn it on?"
        assert found.language == "en"

    @pytest.mark.parametrize(
        "question", [None, "", "?", "x" * (MAX_SENTENCE_CHARS + 1), "a\x00b c"]
    )
    async def test_a_question_that_cannot_be_said_is_not_kept(self, question: str | None) -> None:
        found = await understood(plan(parameters={"state": "on"}, question=question))

        assert isinstance(found, Understood)
        assert found.question is None

    async def test_the_language_is_the_one_there_are_words_for(self) -> None:
        found = await understood(plan(parameters={"state": "on"}, language="it"))

        assert isinstance(found, Understood)
        assert found.language == "it"


class TestCleanSentence:
    @pytest.mark.parametrize(
        ("written", "said"),
        [
            ("Open the door?", "Open the door?"),
            ("  Open   the\tdoor? ", "Open the door?"),
            ("Vuoi che apra il garage?", "Vuoi che apra il garage?"),
            ("OK?", "OK?"),
        ],
    )
    def test_a_sentence_is_one_line(self, written: str, said: str) -> None:
        assert clean_sentence(written) == said

    @pytest.mark.parametrize("written", [None, "", "  ", "ab", "x" * 301, "bell\x07 ring"])
    def test_what_cannot_be_said_is_not(self, written: str | None) -> None:
        assert clean_sentence(written) is None
