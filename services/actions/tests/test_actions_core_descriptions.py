"""What the skills of the platform say of themselves so that a small model tells them apart (ADR
0029).

A model is shown the name and the description of each skill that may be the one that a message asks
for, and the values it takes: nothing else (the examples of a manifest are for the search by
meaning). The platform now has fifteen skills, several of them near each other in meaning, and the
words that tell them apart are what these tests hold. Each says what it is held for: the mistake a
real model made (llama3.1 8B, measured with the corpus of ``factotum.actions.plan_cases``) when they
were not there.
"""

import pytest

from factotum.actions.core import platform_skills
from factotum.actions.testing import FakeSpeech
from factotum.contracts.skills import SkillManifest


@pytest.fixture(scope="module")
def manifests() -> dict[str, SkillManifest]:
    return platform_skills(FakeSpeech()).manifests()


class TestWhatRingsAndWhatIsMuted:
    def test_a_noise_that_stops_is_not_a_cancel_and_is_not_a_mute(
        self, manifests: dict[str, SkillManifest]
    ) -> None:
        """*Stop the timer* was taken for it and *be quiet* for a mute."""
        description = manifests["stop"].description

        assert "does not say which it is" in description
        assert "If the person says timer or alarm it is not this skill" in description
        assert "does not mute the device" in description
        for words in ("enough", "be quiet", "basta", "zitto"):
            assert words in description

    def test_a_timer_that_is_named_is_cancelled(self, manifests: dict[str, SkillManifest]) -> None:
        assert "whenever the person says timer" in manifests["timer-cancel"].description

    def test_an_alarm_that_is_named_is_cancelled(self, manifests: dict[str, SkillManifest]) -> None:
        assert "whenever the person says alarm" in manifests["alarm-cancel"].description


class TestACountdownAndAnAlarm:
    def test_a_reminder_in_some_minutes_is_a_timer(
        self, manifests: dict[str, SkillManifest]
    ) -> None:
        """*Avvisami tra dieci minuti* put an alarm off."""
        assert "remind me in ten minutes" in manifests["timer"].description

    def test_putting_off_is_for_an_alarm_and_never_for_a_new_countdown(
        self, manifests: dict[str, SkillManifest]
    ) -> None:
        description = manifests["alarm-postpone"].description

        assert "never for a new countdown" in description
        for words in ("snooze", "five more minutes", "ancora cinque minuti"):
            assert words in description

    def test_the_next_alarm_is_asked_for_by_when(self, manifests: dict[str, SkillManifest]) -> None:
        """*When is my alarm* was answered by nothing."""
        assert "when is my alarm" in manifests["alarm-status"].description

    def test_the_hour_an_alarm_has_is_only_for_naming_it(
        self, manifests: dict[str, SkillManifest]
    ) -> None:
        """A model moved the *7:30 alarm to 7:30*: it filled in the hour the alarm has with the hour
        it was moved to."""
        description = manifests["alarm-change"].parameters["from"].description or ""

        assert "only when they name that alarm" in description
        assert "it is not the hour it is moved to" in description


class TestTheVolume:
    def test_a_question_about_the_volume_sets_nothing(
        self, manifests: dict[str, SkillManifest]
    ) -> None:
        """*Volume at fifty percent* was taken for a question about the volume."""
        assert "tells and sets nothing" in manifests["volume-status"].description

    def test_the_words_that_were_tried_for_the_level_and_the_turn_are_not_there(
        self, manifests: dict[str, SkillManifest]
    ) -> None:
        """Naming the words of a turn (*louder*, *turn it up*) in the description of
        ``volume-change`` made the model read *alza il volume a 70* as a turn by 70 (measured, in
        both languages): a skill that is described by its examples attracts what is near them."""
        assert "louder" not in manifests["volume-change"].description
        assert "alza" not in manifests["volume"].description
        assert "tells and sets nothing" in manifests["volume-status"].description


class TestTheClock:
    def test_it_does_not_say_that_it_knows_another_place_or_another_day(
        self, manifests: dict[str, SkillManifest]
    ) -> None:
        """*Che giorno è domani* was answered with the date of today."""
        description = manifests["clock"].description

        assert "another place" in description
        assert "another date" in description

    def test_both_is_a_thing_to_ask_for(self, manifests: dict[str, SkillManifest]) -> None:
        """A model that is asked for the time or the date writes *both* when a person asks for both,
        and a choice that is not one of the choices is a value that is missing."""
        assert manifests["clock"].parameters["about"].enum == ("time", "date", "both")
