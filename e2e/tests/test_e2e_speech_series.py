"""The series of commands of ``test_e2e_series.py``, said in Italian by a voice of Piper's and heard
by the real Whisper: what the edge takes each recording for, and what the whole assistant does with
it.

What is real is the voice and the ear. A person says each command, the device sends it as a person
talks, the real Whisper hears it, and what it heard is what the planner is shown to read and what
the device is told it was taken for. What the planner proposes is what a competent one would for the
command that was *meant*, as in the series that has stand-in speech: so a command that Whisper hears
badly is not a command that is then done wrongly, it is a command that is *heard* wrongly, and that
is what is told here, case by case (``-s`` prints how each was heard and how alike it is to what was
said). How much that is a failure is a threshold of what a reader of the words can still tell
apart: each command must be nearer to what was said than to any other of the series.

They are live tests, run by a person with the models of this machine, never in CI and never with a
download (``factotum.e2e.speech`` says where the models are looked for, and how to run Whisper on
the graphics card): ``FACTOTUM_LIVE_TESTS=1 uv run pytest e2e/tests/test_e2e_speech_series.py -s``.
"""

import os

import pytest

from factotum.e2e import devices
from factotum.e2e.devices import Carrier
from factotum.e2e.house import running
from factotum.e2e.models import is_italian
from factotum.e2e.series import (
    SERIES,
    Series,
    devices_of,
    nearest,
    plain,
    problems_of,
    table_of,
)
from factotum.e2e.speech import Person, RealSpeech
from factotum.e2e.stores import Store

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FACTOTUM_LIVE_TESTS") != "1",
        reason="live tests are opt-in: set FACTOTUM_LIVE_TESTS=1",
    ),
]

SLOW_S = 120.0
"""What a test waits for an answer here: a real model on a processor takes seconds."""

ITALIAN = tuple(case for case in SERIES if is_italian(case.message))
"""The part of the series that the house is spoken to in: its speech-to-text binding hears
Italian."""


@pytest.fixture(autouse=True)
def _how_the_house_is_run(monkeypatch: pytest.MonkeyPatch) -> Store:
    """Speech is what these tests are about, and the stores are not: in memory only, and the way
    that will be the real one is the stream (the fixture of ``conftest.py`` by the same name would
    run each test three ways)."""
    monkeypatch.setattr(devices, "DEFAULT_CARRIER", Carrier.STREAM)
    return Store.MEMORY


class TestACommandThatIsSaid:
    async def test_the_commands_in_italian_are_heard_and_come_to_what_they_are_meant_to(
        self, speech: RealSpeech, person: Person
    ) -> None:
        async with (
            running(speech=speech, timeout_s=SLOW_S, offer_every_skill=True) as house,
            devices_of(house) as devices_,
        ):
            series = Series(
                house,
                devices_,
                lambda device, words: device.sends(person.saying(words)),
                readable=False,  # what a voice says is not read back out of the sound
            )

            played = await series.run(ITALIAN)

            print(f"\n  Whisper model: {speech.whisper}")
            print("\n".join(f"  {line}" for line in table_of(played)))
            # What each came to, from what was heard of it...
            wrong = problems_of(played)
            assert not wrong, "\n".join(wrong)
            # ...which is what the planner was shown to read, a message and no more.
            heard = [each.first.heard for each in played]
            assert [shown.rsplit("Message: ", 1)[-1] for shown in house.model.plans] == heard
            assert house.whisper.languages == [("it",)] * len(house.whisper.heard)
            # And what was heard is near enough to be told from the rest: a reader who knows the
            # commands takes each for the one that was said.
            said = [case.message for case in ITALIAN]
            mistaken = [
                f"{message!r} was heard as {text!r}, nearer to {nearest(text or '', said)!r}"
                for message, text in zip(said, heard, strict=True)
                if nearest(text or "", said) != message
            ]
            assert not mistaken, "\n".join(mistaken)
            # A yes and a no are heard as what they are: a yes opens, and a no leaves things shut.
            for each in played:
                if each.answer is not None:
                    first_word = plain(each.answer.heard or "").split()[:1]
                    assert first_word == plain(each.answered_with or "").split()[:1], each.answer
