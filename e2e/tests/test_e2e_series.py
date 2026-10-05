"""A series of commands, said to the three devices of the example home one after another, and what
the whole assistant did with each: more than eighty messages, in English and in Italian, that ask
for a lamp, the garage door, the volume, the weather, the air, an announcement, a scene for a movie
and the router to be dealt with, and questions that look like them (``factotum.e2e.series``).

Each is said the way the device carries sound (a recording whole, or as a person talks, and the
voice of the answer a clip or a stream), on the one connection that each device holds for the whole
series, so that what is tried is also that a connection which has been through eighty answers is
what it was at the first. The planner is a stand-in that proposes what a competent one would (what
is looked at is the rest of the assistant); the same series is said with real speech, in Italian,
in ``test_e2e_speech_series.py``.
"""

from collections import Counter

from factotum.actions.plan_cases import DISPLAY, KITCHEN, PHONE
from factotum.client_core.testing import FakeSpeaker
from factotum.contracts.actions import ActStatus, AuditList
from factotum.e2e.house import House, running
from factotum.e2e.series import SERIES, Series, as_steps, devices_of, problems_of, trail_of


async def trail(house: House) -> AuditList:
    listed = await house.actions.get("/v1/audit", params={"limit": 200}, headers=house.admin)
    assert listed.status_code == 200, listed.text
    return AuditList.model_validate(listed.json())


class TestTheCorpus:
    def test_it_is_a_series_worth_saying(self) -> None:
        by_status = Counter(case.status for case in SERIES)

        assert len(SERIES) >= 80
        assert set(by_status) == set(ActStatus) - {ActStatus.CANCELLED, ActStatus.FAILED}
        assert {case.speaker for case in SERIES} == {KITCHEN, DISPLAY, PHONE}


class TestASeriesOfCommands:
    async def test_every_message_comes_to_what_it_is_meant_to_on_connections_that_last(
        self,
    ) -> None:
        async with running(offer_every_skill=True) as house, devices_of(house) as devices:
            series = Series(house, devices, lambda device, words: device.says_aloud(words))

            played = await series.run(SERIES)

            # Each came to what the corpus says: done, asked about first, refused, asked for what
            # was missing, or left as a question, and the commands did what they were for...
            wrong = problems_of(played)
            assert not wrong, "\n".join(wrong)
            # ...from what the edge took each recording for, which is what was said, and which is
            # what the planner was shown to read, a message and no more...
            messages = [case.message for case in SERIES]
            assert [each.first.heard for each in played] == messages
            assert [shown.rsplit("Message: ", 1)[-1] for shown in house.model.plans] == messages
            # ...what was done, refused and asked about is in the trail, in the order it was said,
            # and nothing else is (what asks for more, and what is only a question, leaves none)...
            assert as_steps((await trail(house)).records) == trail_of(played)
            # ...and a connection that has been through all of it is what it was at the first.
            for name, device in devices.items():
                assert device.shown.of("violation") == [], name  # the edge broke no rule
                assert device.shown.of("error") == [], name
                assert not device.client.playing, name  # nothing is left being said
                if isinstance(device.speaker, FakeSpeaker):
                    # every voice that was streamed was streamed to its end
                    assert all(track.finished for track in device.speaker.tracks), name
