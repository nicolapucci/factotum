"""What a device says of itself, a line for each thing: and what it leaves out, and when."""

import io

from pydantic import AnyHttpUrl

from factotum.contracts.cards import Card
from factotum.contracts.edge import ErrorFrame, TurnEnded, TurnStatus
from factotum.contracts.response import StatusState
from factotum.device.render import DeviceRenderer


def written_by(quiet: bool = False) -> tuple[DeviceRenderer, io.StringIO]:
    out = io.StringIO()
    return DeviceRenderer(out, quiet=quiet), out


class TestWhatIsWritten:
    def test_each_thing_is_a_line_with_a_tag_that_says_which_it_is(self) -> None:
        renderer, out = written_by()

        renderer.heard("apri il garage")
        renderer.speech("Do you really want me to open the garage door?")
        renderer.status(StatusState.SEARCHING)
        renderer.event("volume 30")
        renderer.notice("listening")

        assert out.getvalue().splitlines() == [
            "[heard]  apri il garage",
            "[say]    Do you really want me to open the garage door?",
            "[...]    searching",
            "[do]     volume 30",
            "--       listening",
        ]

    def test_a_failed_answer_says_why_and_a_cancelled_one_that_it_was_interrupted(self) -> None:
        renderer, out = written_by()

        renderer.turn_ended(
            TurnEnded(
                utterance_id="u1", status=TurnStatus.FAILED, code="model_busy", message="try later"
            )
        )
        renderer.turn_ended(TurnEnded(utterance_id="u2", status=TurnStatus.CANCELLED))
        renderer.turn_ended(TurnEnded(utterance_id="u3", status=TurnStatus.FINISHED))

        assert out.getvalue().splitlines() == [
            "[!]      the answer failed (model_busy): try later",
            "[stop]   interrupted",
        ]

    def test_a_failed_answer_that_says_nothing_more_is_not_followed_by_a_colon(self) -> None:
        renderer, out = written_by()

        renderer.turn_ended(
            TurnEnded(utterance_id="u1", status=TurnStatus.FAILED, code="model_busy")
        )
        renderer.turn_ended(TurnEnded(utterance_id="u2", status=TurnStatus.FAILED))

        assert out.getvalue().splitlines() == [
            "[!]      the answer failed (model_busy)",
            "[!]      the answer failed",
        ]

    def test_a_line_is_flushed_as_it_is_written(self) -> None:
        # Standard output is a pipe under a service, and a pipe is not written until it is full.
        class Flushes(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.count = 0

            def flush(self) -> None:
                self.count += 1

        out = Flushes()
        renderer = DeviceRenderer(out)

        renderer.notice("one line")
        renderer.speech("and\nanother")

        assert out.count == 2

    def test_what_is_wrong_is_loud_whoever_it_is_that_got_it_wrong(self) -> None:
        renderer, out = written_by()

        renderer.error(ErrorFrame(code="auth_failed", message="who are you?", fatal=True))
        renderer.violation("the edge sent sound that nobody asked for")

        assert out.getvalue().splitlines() == [
            "[edge!]  auth_failed: who are you?",
            "[BUG]    the edge sent sound that nobody asked for",
        ]

    def test_a_line_that_goes_on_is_lined_up_under_its_tag(self) -> None:
        renderer, out = written_by()

        renderer.speech("first\nsecond")

        assert out.getvalue() == "[say]    first\n         second\n"

    def test_a_speaker_shows_no_text_no_card_and_no_music(self) -> None:
        renderer, out = written_by()

        renderer.text("text")
        renderer.card(Card(title="A card"))
        renderer.media("Radio", str(AnyHttpUrl("https://radio.example.org/s")))

        assert out.getvalue() == ""


class TestWhenItIsToBeQuiet:
    def test_what_is_said_is_heard_and_so_not_written_but_what_went_wrong_is(self) -> None:
        renderer, out = written_by(quiet=True)

        renderer.speech("what is said aloud")
        renderer.status(StatusState.THINKING)
        renderer.turn_ended(TurnEnded(utterance_id="u1", status=TurnStatus.CANCELLED))
        renderer.heard("what was heard")
        renderer.violation("a bug")
        renderer.notice("connected")

        assert out.getvalue().splitlines() == [
            "[heard]  what was heard",
            "[BUG]    a bug",
            "--       connected",
        ]
