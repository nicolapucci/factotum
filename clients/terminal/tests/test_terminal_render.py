import io

import pytest

from factotum.contracts.cards import Card, CardItem
from factotum.contracts.edge import ErrorFrame, TurnEnded, TurnStatus
from factotum.contracts.response import StatusState
from factotum.terminal.render import ConsoleRenderer


def rendered(action: str, *args: object) -> str:
    out = io.StringIO()
    getattr(ConsoleRenderer(out), action)(*args)
    return out.getvalue()


class TestWhatADeviceDoes:
    def test_what_is_said_is_shown_with_the_tag_of_the_speaker(self) -> None:
        assert rendered("speech", "Hold the button for ten seconds.") == (
            "[say]    Hold the button for ten seconds.\n"
        )

    def test_what_is_shown_is_shown_with_the_tag_of_the_screen(self) -> None:
        assert rendered("text", "Hold the button [1].") == "[show]   Hold the button [1].\n"

    def test_a_card_is_a_title_a_body_and_a_list(self) -> None:
        card = Card(
            title="Sources",
            body="Two documents",
            items=(CardItem(label="[1] Router manual", value="page 12"), CardItem(label="[2] FAQ")),
        )

        assert rendered("card", card) == (
            "[card]   Sources\n"
            "         Two documents\n"
            "         - [1] Router manual: page 12\n"
            "         - [2] FAQ\n"
        )

    def test_media_is_shown_with_its_title_when_it_has_one(self) -> None:
        assert rendered("media", "Radio", "https://radio.example.org/s") == (
            "[play]   Radio (https://radio.example.org/s)\n"
        )
        assert rendered("media", None, "https://radio.example.org/s") == (
            "[play]   https://radio.example.org/s\n"
        )

    def test_progress_is_a_hint(self) -> None:
        assert rendered("status", StatusState.SEARCHING) == "[...]    searching\n"

    def test_a_command_that_was_run_is_said(self) -> None:
        assert rendered("event", "volume 30") == "[do]     volume 30\n"

    def test_a_line_that_goes_on_is_aligned_with_the_first(self) -> None:
        assert rendered("speech", "one\ntwo") == "[say]    one\n         two\n"

    def test_nothing_is_still_a_line(self) -> None:
        assert rendered("speech", "") == "[say]    \n"


class TestHowAnAnswerEnded:
    def test_an_answer_that_went_well_says_nothing_more(self) -> None:
        ended = TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED)

        assert rendered("turn_ended", ended) == ""

    def test_an_answer_that_failed_says_why(self) -> None:
        ended = TurnEnded(
            utterance_id="u1",
            status=TurnStatus.FAILED,
            code="chat_not_configured",
            message="no chat model is set up",
        )

        assert rendered("turn_ended", ended) == (
            "[!]      the answer failed (chat_not_configured): no chat model is set up\n"
        )

    def test_an_answer_that_failed_without_saying_why_still_says_it_failed(self) -> None:
        ended = TurnEnded(utterance_id="u1", status=TurnStatus.FAILED)

        assert rendered("turn_ended", ended) == "[!]      the answer failed\n"

    def test_an_answer_that_was_stopped_says_so(self) -> None:
        ended = TurnEnded(utterance_id="u1", status=TurnStatus.CANCELLED)

        assert rendered("turn_ended", ended) == "[stop]   interrupted\n"


class TestWhatIsWrong:
    def test_what_the_edge_says_is_wrong_is_said(self) -> None:
        error = ErrorFrame(code="auth_failed", message="the device is not known")

        assert rendered("error", error) == "[edge!]  auth_failed: the device is not known\n"

    def test_what_the_edge_does_that_it_must_not_is_said_loudly(self) -> None:
        assert rendered("violation", "a part came out of order") == (
            "[BUG]    a part came out of order\n"
        )

    def test_the_terminal_speaks_for_itself_with_a_dash(self) -> None:
        assert rendered("notice", "connected") == "--       connected\n"


@pytest.mark.parametrize(
    "action",
    ["speech", "text", "event", "violation", "notice"],
)
def test_what_is_written_is_written_at_once_and_not_kept(action: str) -> None:
    class Flushing(io.StringIO):
        flushed = 0

        def flush(self) -> None:
            self.flushed += 1

    out = Flushing()

    getattr(ConsoleRenderer(out), action)("something")

    assert out.flushed == 1
