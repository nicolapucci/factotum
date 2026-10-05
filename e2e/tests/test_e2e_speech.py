"""The assistant end to end with real speech: somebody says something in Italian with a voice of
Piper's, the real Whisper hears it, the assistant understands it and does what it asks, and the
real Piper says the answer, which a Whisper listens to again.

What is real is what these tests are about: the transcription (what was heard, and in which
language), the routing (what was understood of it, and what was attempted because of it), and the
synthesis (what the speaker was made to say, as a person who listens to it hears it). What is made
up is the rest, as in the other end-to-end tests: the hardware of the device, the models that write
and plan (so what they propose is what a competent one would), and what the skills reach, which
need not exist for the right thing to have been attempted.

They are live tests, run by a person with the models of this machine, never in CI and never with a
download: ``FACTOTUM_LIVE_TESTS=1 uv run pytest e2e -m live -s`` (``factotum.e2e.speech`` says where
the models are looked for, and how to run Whisper on the graphics card). A real model hears a voice
a little differently on each device, so what is looked for in what it heard is the stem of a word,
not a sentence.
"""

import difflib
import os
import re
import unicodedata
import wave
from collections.abc import Iterable
from io import BytesIO

import pytest

from factotum.contracts.audio import AudioPayload
from factotum.contracts.edge import TurnStatus
from factotum.contracts.knowledge import CollectionOwner
from factotum.e2e.devices import Device, connected
from factotum.e2e.house import House, running
from factotum.e2e.speech import (
    MICROPHONE_RATE,
    Person,
    RealSpeech,
    find_speech,
    hears,
    load,
    silence,
)
from factotum.e2e.stores import Store

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FACTOTUM_LIVE_TESTS") != "1",
        reason="live tests are opt-in: set FACTOTUM_LIVE_TESTS=1",
    ),
]

SLOW_S = 120.0
"""What a test waits for an answer here: a real model on a processor takes seconds, and a card that
something else is using takes more."""

HOME_ASSISTANT = "http://homeassistant.local:8123"
WASHER = (
    "Per pulire il filtro della lavatrice apri lo sportello in basso a destra e svita il tappo."
)
QUESTION = "Come si pulisce il filtro della lavatrice?"


@pytest.fixture(autouse=True)
def _where_the_services_keep_what_they_keep() -> Store:
    """Speech is what these tests are about, and the stores are not: in memory only. (This is the
    fixture of ``conftest.py`` by the same name, which would run each test on Postgres too.)"""
    return Store.MEMORY


@pytest.fixture(scope="session")
def speech() -> RealSpeech:
    found = find_speech()
    if found is None:
        pytest.skip("no Whisper model and Piper voice on this machine, and none is downloaded")
    load(found)  # the models are loaded once, here, and not by the first test that needs them
    return found


@pytest.fixture
def person(speech: RealSpeech) -> Person:
    return Person(speech)


def plain(text: str) -> str:
    """What was heard, in lower case, without accents or punctuation: to look for a stem in."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", letters)


def assert_says(heard: str | Iterable[str], *stems: str) -> None:
    text = plain(heard if isinstance(heard, str) else " ".join(heard))
    missing = [stem for stem in stems if stem not in text]
    assert not missing, f"{missing} are not in what was heard: {heard!r}"


def assert_understood(heard: list[str], said: list[str], *, at_least: float = 0.75) -> None:
    """What somebody who listens to the voice hears, sentence by sentence, is what the voice was
    asked to say, give or take what a small model makes of a word or two."""
    assert len(heard) == len(said), f"{said!r} was heard as {heard!r}"
    for listened, asked in zip(heard, said, strict=True):
        alike = difflib.SequenceMatcher(None, plain(listened), plain(asked)).ratio()
        assert alike >= at_least, f"{asked!r} was heard as {listened!r} ({alike:.2f} alike)"


def seconds(clip: AudioPayload) -> float:
    with wave.open(BytesIO(clip.data), "rb") as wav_file:
        return wav_file.getnframes() / wav_file.getframerate()


def reached(house: House) -> list[str]:
    """What Home Assistant was asked, as the call and the entity, in order."""
    return [
        f"{seen.target.removeprefix(HOME_ASSISTANT)} {seen.json()['entity_id']}"
        for seen in house.home_assistant.seen
    ]


def listens(speech: RealSpeech, device: Device) -> list[str]:
    return device.heard_aloud(lambda clip: hears(speech, clip))


class TestAQuestionByVoice:
    async def test_a_question_in_italian_is_heard_found_in_the_documents_and_answered_aloud(
        self, speech: RealSpeech, person: Person
    ) -> None:
        async with running(speech=speech, timeout_s=SLOW_S) as house:
            await house.library.add("Casa", "Lavatrice", WASHER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.sends(person.saying(QUESTION))

                assert ended.status is TurnStatus.FINISHED, ended
                # It was heard in Italian, whatever the model made of the words...
                [heard] = house.whisper.heard
                assert_says(heard, "filt", "lavat")
                assert house.whisper.languages == [("it",)]
                # ...it reached the model that writes the answer with the document in front of it...
                [prompt] = house.model.prompts
                assert WASHER in prompt
                # ...and what was written was said, in the voice that was bound, and is understood
                # by somebody who listens: the sentence of the document, in Italian.
                assert len(house.piper.spoken) == 2, house.piper.spoken
                assert all(seconds(clip) > 0.5 for clip in speaker.speaker.played)
                assert_understood(listens(speech, speaker), house.piper.spoken)

    async def test_a_question_that_is_typed_is_answered_aloud_in_the_same_voice(
        self, speech: RealSpeech
    ) -> None:
        async with running(speech=speech, timeout_s=SLOW_S) as house:
            await house.library.add("Casa", "Lavatrice", WASHER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.types(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                assert house.whisper.heard == []  # nothing was a recording
                assert_understood(listens(speech, speaker), house.piper.spoken)

    async def test_a_question_that_no_document_answers_is_answered_that_none_does(
        self, speech: RealSpeech, person: Person
    ) -> None:
        async with (
            running(speech=speech, timeout_s=SLOW_S) as house,
            connected(house, "speaker") as speaker,
        ):
            ended = await speaker.sends(person.saying("Come si pulisce il filtro?"))

            assert ended.status is TurnStatus.FINISHED, ended
            assert_says(listens(speech, speaker), "trovato", "document")


class TestAnActionByVoice:
    async def test_switching_a_lamp_on_is_understood_and_attempted(
        self, speech: RealSpeech, person: Person
    ) -> None:
        async with (
            running(speech=speech, timeout_s=SLOW_S) as house,
            connected(house, "speaker") as speaker,
        ):
            ended = await speaker.sends(person.saying("Accendi la luce del soggiorno."))

            assert ended.status is TurnStatus.FINISHED, ended
            [heard] = house.whisper.heard
            assert_says(heard, "accend", "luc")
            # The lamp of the living room was asked for, through the skill that is bound to it (that
            # Home Assistant is made up does not matter: the right thing was attempted).
            assert reached(house) == ["/api/services/light/turn_on light.living_room"]
            assert speaker.speaker.played, "nothing was said about it"

    async def test_a_number_that_was_said_is_the_number_that_is_set(
        self, speech: RealSpeech, person: Person
    ) -> None:
        async with (
            running(speech=speech, timeout_s=SLOW_S) as house,
            connected(house, "speaker") as speaker,
        ):
            ended = await speaker.sends(person.saying("Imposta il volume a trenta."))

            assert ended.status is TurnStatus.FINISHED, ended
            assert_says(house.whisper.heard, "volum")
            assert speaker.hardware.state.volume == 30

    async def test_the_garage_door_is_asked_about_and_a_spoken_yes_opens_it(
        self, speech: RealSpeech, person: Person
    ) -> None:
        async with (
            running(speech=speech, timeout_s=SLOW_S) as house,
            connected(house, "smart-display") as display,
        ):
            asked = await display.sends(person.saying("Apri il garage."))
            opened_before_the_yes = reached(house)
            answered = await display.sends(person.saying("Sì."))

            assert asked.status is TurnStatus.FINISHED, asked
            assert answered.status is TurnStatus.FINISHED, answered
            assert opened_before_the_yes == []
            # What was asked first is what the planner proposes for a door that needs a yes, said
            # in Italian (how a small model hears the loanword in it is not what is tried here).
            assert house.piper.spoken[0] == "Vuoi davvero che apra il garage?"
            assert seconds(display.speaker.played[0]) > 0.8
            assert plain(house.whisper.heard[-1]).split()[:1] == ["si"], house.whisper.heard
            assert reached(house) == ["/api/services/cover/open_cover cover.garage"]

    async def test_a_spoken_no_leaves_the_door_shut(
        self, speech: RealSpeech, person: Person
    ) -> None:
        async with (
            running(speech=speech, timeout_s=SLOW_S) as house,
            connected(house, "smart-display") as display,
        ):
            await display.sends(person.saying("Apri il garage."))
            answered = await display.sends(person.saying("No, grazie."))

            assert answered.status is TurnStatus.FINISHED, answered
            assert reached(house) == []

    async def test_something_that_is_neither_yes_nor_no_does_not_open_it(
        self, speech: RealSpeech, person: Person
    ) -> None:
        async with (
            running(speech=speech, timeout_s=SLOW_S) as house,
            connected(house, "smart-display") as display,
        ):
            await display.sends(person.saying("Apri il garage."))
            answered = await display.sends(person.saying("Forse più tardi."))

            assert answered.status is TurnStatus.FINISHED, answered
            assert reached(house) == []

    async def test_the_speaker_of_the_kitchen_is_refused_the_garage_however_it_is_asked(
        self, speech: RealSpeech, person: Person
    ) -> None:
        async with (
            running(speech=speech, timeout_s=SLOW_S) as house,
            connected(house, "speaker") as speaker,
        ):
            ended = await speaker.sends(person.saying("Apri il garage."))

            assert ended.status is TurnStatus.FINISHED, ended
            assert reached(house) == []
            assert speaker.speaker.played, "the refusal was not said"


class TestSomethingThatIsNotASentence:
    async def test_a_second_of_silence_is_not_a_message(self, speech: RealSpeech) -> None:
        async with (
            running(speech=speech, timeout_s=SLOW_S) as house,
            connected(house, "speaker") as speaker,
        ):
            ended = await speaker.sends(
                AudioPayload(sample_rate=MICROPHONE_RATE, data=silence(1.0))
            )

            assert (ended.status, ended.code) == (TurnStatus.FAILED, "nothing_heard")
            assert house.model.prompts == []
