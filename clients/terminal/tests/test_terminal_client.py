import asyncio
from typing import Any
from uuid import uuid4

import pytest
from pydantic import AnyHttpUrl

from factotum.contracts.audio import AudioPayload
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import (
    CommandStatus,
    LedPattern,
    Pause,
    SetLed,
    SetVolume,
    ShowCard,
)
from factotum.contracts.edge import (
    PROTOCOL_VERSION,
    CommandFrame,
    ErrorFrame,
    PartFrame,
    Pong,
    Ready,
    TurnEnded,
    TurnStatus,
)
from factotum.contracts.ids import DeviceId
from factotum.contracts.response import (
    CardPart,
    CommandPart,
    MediaPart,
    Speech,
    StatusPart,
    StatusState,
    Text,
)
from factotum.terminal.client import CannotConverse, DeviceClient, Refused
from factotum.terminal.connection import ConnectionLost
from factotum.terminal.device import TerminalDevice
from factotum.terminal.profiles import Profile, get_profile
from factotum.terminal.testing import FakeVoiceOutput, MemoryConnection, RecordingRenderer

RECORDING = AudioPayload(sample_rate=16000, data=b"raw-microphone-bytes")


class Rig:
    def __init__(
        self, profile: str = "speaker", *, voice_output: FakeVoiceOutput | None = None
    ) -> None:
        self.profile = get_profile(profile)
        self.connection = MemoryConnection()
        self.shown = RecordingRenderer()
        self.hardware = TerminalDevice(self.profile.capabilities, self.shown)
        self.client = DeviceClient(
            self.connection, self.profile, self.shown, self.hardware, voice_output=voice_output
        )

    def ready(self, **changes: Any) -> Ready:
        fields: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": "s1",
            "device_id": DeviceId(uuid4()),
            "name": "Kitchen speaker",
            "capabilities": self.profile.capabilities,
        }
        return Ready(**{**fields, **changes})

    async def connected(self, **changes: Any) -> "Rig":
        self.connection.say(self.ready(**changes))
        await self.client.connect()
        return self

    async def running(self) -> "asyncio.Task[Any]":
        await self.connected()
        return asyncio.create_task(self.client.run())


def part(seq: int, what: Any, utterance: str = "u1") -> PartFrame:
    return PartFrame(utterance_id=utterance, seq=seq, part=what)


async def settle() -> None:
    """Let what is waiting to be done be done."""
    for _ in range(5):
        await asyncio.sleep(0)


class TestSayingHello:
    async def test_it_says_what_this_device_is_and_what_it_can_do(self) -> None:
        rig = Rig("smart-display")
        rig.connection.say(rig.ready(name="Living room display"))

        await rig.client.connect()

        [hello] = rig.connection.frames("hello")
        assert (hello.hardware_id, hello.token, hello.protocol_version) == (
            "living-room-01",
            "living-room-dev-token",
            PROTOCOL_VERSION,
        )
        assert hello.capabilities == rig.profile.capabilities
        assert hello.display == rig.profile.display
        assert (hello.client.name if hello.client else None) == "factotum-terminal"
        await rig.client.close()

    async def test_another_identity_can_be_presented(self) -> None:
        rig = Rig()
        client = DeviceClient(
            rig.connection,
            rig.profile,
            rig.shown,
            rig.hardware,
            hardware_id="lab-9",
            token="lab-credential",
        )
        rig.connection.say(rig.ready())

        await client.connect()

        [hello] = rig.connection.frames("hello")
        assert (hello.hardware_id, hello.token) == ("lab-9", "lab-credential")
        await client.close()

    async def test_it_says_what_it_was_told_it_is(self) -> None:
        rig = Rig()

        await rig.connected(name="Kitchen speaker")

        assert rig.shown.of("notice") == ["connected as Kitchen speaker (audio_in, audio_out, led)"]
        assert rig.client.ready is not None
        assert rig.client.ready.name == "Kitchen speaker"
        await rig.client.close()

    async def test_it_says_what_the_edge_will_not_use(self) -> None:
        rig = Rig()

        await rig.connected(
            capabilities=frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT}),
            unregistered=frozenset({Capability.LED}),
        )

        assert rig.shown.of("notice")[1] == "not registered for led: the edge will not use it"
        await rig.client.close()

    async def test_a_device_the_edge_does_not_know_is_refused_with_the_reason(self) -> None:
        rig = Rig()
        rig.connection.say(ErrorFrame(code="auth_failed", message="not known", fatal=True))

        with pytest.raises(Refused) as refused:
            await rig.client.connect()

        assert (refused.value.code, refused.value.message) == ("auth_failed", "not known")

    @pytest.mark.parametrize("what", ["not json", '{"type": "dance"}'])
    async def test_what_is_not_a_frame_is_a_refusal(self, what: str) -> None:
        rig = Rig()
        rig.connection.say(what)

        with pytest.raises(Refused) as refused:
            await rig.client.connect()

        assert refused.value.code == "invalid_frame"

    async def test_an_edge_that_says_something_else_first_is_refused(self) -> None:
        rig = Rig()
        rig.connection.say(Pong())

        with pytest.raises(Refused, match="did not say it was ready"):
            await rig.client.connect()

    async def test_a_connection_that_ends_before_the_answer_is_a_refusal(self) -> None:
        rig = Rig()
        rig.connection.drop(1008, "policy")

        with pytest.raises(Refused) as refused:
            await rig.client.connect()

        assert refused.value.code == "connection_lost"
        assert refused.value.message == "policy"


class TestKeepingInTouch:
    async def test_it_says_it_is_there_as_often_as_it_was_asked_to(self) -> None:
        rig = Rig()
        await rig.connected(heartbeat_s=1)

        await rig.connection.wait_for("ping", count=2, timeout_s=3)

        assert len(rig.connection.frames("ping")) >= 2
        await rig.client.close()

    async def test_it_stops_when_the_connection_does(self) -> None:
        rig = Rig()
        await rig.connected(heartbeat_s=1)
        running = asyncio.create_task(rig.client.run())

        rig.connection.drop()
        lost = await running
        await asyncio.sleep(1.2)

        assert isinstance(lost, ConnectionLost)
        assert rig.connection.frames("ping") == []


class TestWhatAnAnswerIsMadeOf:
    async def test_a_speaker_says_what_it_is_sent_and_shows_a_hint(self) -> None:
        rig = Rig("speaker")
        running = await rig.running()
        identity = await rig.client.say("how do I reset the router?")

        rig.connection.say(part(0, StatusPart(state=StatusState.SEARCHING)))
        rig.connection.say(part(1, Speech(text="Hold the button for ten seconds.")))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        ended = await rig.client.wait_for_turn(identity)

        assert rig.shown.kinds() == ["notice", "status", "speech", "turn_ended"]
        assert rig.shown.of("speech") == ["Hold the button for ten seconds."]
        assert rig.shown.of("status") == [StatusState.SEARCHING]
        assert ended is not None
        assert ended.status is TurnStatus.FINISHED
        running.cancel()

    async def test_a_screen_shows_text_a_card_and_plays_media(self) -> None:
        rig = Rig("smart-display")
        running = await rig.running()
        identity = await rig.client.say("hello")
        card = Card(title="Sources")

        rig.connection.say(part(0, Text(text="Hold the button [1].")))
        rig.connection.say(part(1, CardPart(card=card)))
        rig.connection.say(
            part(2, MediaPart(url=AnyHttpUrl("https://radio.example.org/s"), title="Radio"))
        )
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)

        assert rig.shown.of("text") == ["Hold the button [1]."]
        assert rig.shown.of("card") == [card]
        # media needs a media player, which a smart display does not have: that is a mistake
        assert any("media_player" in v for v in rig.shown.of("violation"))
        running.cancel()

    async def test_a_command_in_an_answer_is_run_and_a_failing_one_is_said(self) -> None:
        rig = Rig("speaker")
        running = await rig.running()
        identity = await rig.client.say("hello")

        rig.connection.say(part(0, CommandPart(command=SetVolume(level=30))))
        rig.connection.say(part(1, CommandPart(command=SetLed(pattern=LedPattern.BLINK))))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)

        assert rig.hardware.state.volume == 30
        assert rig.hardware.state.led_pattern is LedPattern.BLINK
        assert rig.shown.of("violation") == []
        running.cancel()

    async def test_an_answer_that_failed_or_was_stopped_is_shown_as_it_ended(self) -> None:
        rig = Rig()
        running = await rig.running()
        first = await rig.client.say("one")
        rig.connection.say(
            TurnEnded(utterance_id=first, status=TurnStatus.FAILED, code="x", message="why")
        )
        await rig.client.wait_for_turn(first)
        second = await rig.client.say("two")
        rig.connection.say(TurnEnded(utterance_id=second, status=TurnStatus.CANCELLED))

        ended = await rig.client.wait_for_turn(second)

        assert ended is not None
        assert [e.status for e in rig.shown.of("turn_ended")] == [
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
        ]
        running.cancel()


class TestWhenTheEdgeIsWrong:
    """A device that saw any of this would say so: that is what the client is for."""

    async def test_a_part_the_hardware_cannot_render_is_not_rendered_and_is_reported(self) -> None:
        rig = Rig("speaker")  # it has no screen
        running = await rig.running()
        identity = await rig.client.say("hello")

        rig.connection.say(part(0, Text(text="Something to read")))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)

        assert rig.shown.of("text") == []
        assert rig.shown.of("violation") == ["the edge sent a text to a device that has no display"]
        running.cancel()

    async def test_parts_out_of_order_are_reported(self) -> None:
        rig = Rig()
        running = await rig.running()
        identity = await rig.client.say("hello")

        rig.connection.say(part(0, Speech(text="one")))
        rig.connection.say(part(2, Speech(text="three")))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)

        [violation] = rig.shown.of("violation")
        assert "out of order" in violation
        assert "1 was expected, 2 came" in violation
        running.cancel()

    async def test_an_answer_that_nobody_asked_for_is_reported_and_not_rendered(self) -> None:
        rig = Rig()
        running = await rig.running()

        rig.connection.say(part(0, Speech(text="unasked"), utterance="never-asked"))
        await settle()

        assert rig.shown.of("speech") == []
        assert "nobody is waiting for" in rig.shown.of("violation")[0]
        running.cancel()

    async def test_a_part_after_the_answer_ended_is_reported(self) -> None:
        rig = Rig()
        running = await rig.running()
        identity = await rig.client.say("hello")
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)

        rig.connection.say(part(0, Speech(text="late")))
        await settle()

        assert rig.shown.of("speech") == []
        assert len(rig.shown.of("violation")) == 1
        running.cancel()

    async def test_what_is_not_a_frame_is_reported_and_what_follows_is_still_heard(self) -> None:
        rig = Rig()
        running = await rig.running()
        identity = await rig.client.say("hello")

        rig.connection.say("garbage")
        rig.connection.say(part(0, Speech(text="still here")))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)

        assert rig.shown.of("speech") == ["still here"]
        assert "not a frame" in rig.shown.of("violation")[0]
        running.cancel()

    async def test_ready_twice_is_reported(self) -> None:
        rig = Rig()
        running = await rig.running()

        rig.connection.say(rig.ready())
        await settle()

        assert rig.shown.of("violation") == ["the edge said ready a second time"]
        running.cancel()

    async def test_what_the_edge_says_is_wrong_is_shown_and_a_pong_is_nothing(self) -> None:
        rig = Rig()
        running = await rig.running()

        rig.connection.say(ErrorFrame(code="capability_missing", message="no mic", ref="u1"))
        rig.connection.say(Pong())
        await settle()

        assert [e.code for e in rig.shown.of("error")] == ["capability_missing"]
        assert rig.shown.of("violation") == []
        running.cancel()


class TestCommandsFromTheEdge:
    async def test_a_command_is_run_and_what_became_of_it_is_answered_with_its_id(self) -> None:
        rig = Rig()
        running = await rig.running()

        rig.connection.say(CommandFrame(id="c1", command=SetVolume(level=70)))
        [ack] = await rig.connection.wait_for("ack")

        assert (ack.id, ack.result.status) == ("c1", CommandStatus.OK)
        assert rig.hardware.state.volume == 70
        running.cancel()

    async def test_what_the_hardware_cannot_do_is_answered_as_unsupported(self) -> None:
        rig = Rig("screen")  # a display and nothing else
        running = await rig.running()

        rig.connection.say(CommandFrame(id="c1", command=SetVolume(level=70)))
        [ack] = await rig.connection.wait_for("ack")

        assert ack.result.status is CommandStatus.UNSUPPORTED
        running.cancel()

    async def test_what_goes_wrong_is_answered_as_failed(self) -> None:
        rig = Rig("player")
        running = await rig.running()

        rig.connection.say(CommandFrame(id="c1", command=Pause()))  # nothing is playing
        [ack] = await rig.connection.wait_for("ack")

        assert (ack.result.status, ack.result.detail) == (
            CommandStatus.FAILED,
            "nothing is playing",
        )
        running.cancel()

    async def test_a_card_reaches_a_screen_and_is_acknowledged(self) -> None:
        rig = Rig("screen")
        running = await rig.running()

        rig.connection.say(CommandFrame(id="c1", command=ShowCard(card=Card(title="Recipe"))))
        [ack] = await rig.connection.wait_for("ack")

        assert ack.result.succeeded
        assert rig.hardware.state.card == Card(title="Recipe")
        running.cancel()

    async def test_an_answer_that_cannot_be_sent_does_not_end_the_client(self) -> None:
        rig = Rig()
        running = await rig.running()
        rig.connection.closed = True  # the way back is closed

        rig.connection.say(CommandFrame(id="c1", command=SetVolume(level=70)))
        await settle()

        assert not running.done()
        assert rig.hardware.state.volume == 70
        running.cancel()


class TestSpeaking:
    async def test_what_is_said_is_sent_as_an_utterance_and_each_has_its_own_name(self) -> None:
        rig = Rig()
        await rig.connected()

        first = await rig.client.say("how do I reset the router?")
        second = await rig.client.say("and in Italian?")

        assert (first, second) == ("u1", "u2")
        assert [(u.id, u.text) for u in rig.connection.frames("utterance")] == [
            ("u1", "how do I reset the router?"),
            ("u2", "and in Italian?"),
        ]
        assert rig.client.active == "u2"
        await rig.client.close()

    async def test_a_device_with_no_microphone_cannot_start_a_conversation(self) -> None:
        rig = Rig("screen")
        await rig.connected()

        with pytest.raises(CannotConverse, match="no microphone"):
            await rig.client.say("hello?")

        assert rig.connection.frames("utterance") == []
        await rig.client.close()

    async def test_it_can_be_made_to_say_it_anyway_to_see_what_the_edge_makes_of_it(self) -> None:
        rig = Rig("screen")
        await rig.connected()

        await rig.client.say("hello?", force=True)

        assert len(rig.connection.frames("utterance")) == 1
        await rig.client.close()

    async def test_a_recording_is_sent_as_an_audio_utterance(self) -> None:
        rig = Rig()
        await rig.connected()

        identity = await rig.client.say_audio(RECORDING)

        assert identity == "u1"
        assert [u.audio for u in rig.connection.frames("audio_utterance")] == [RECORDING]
        assert rig.client.active == "u1"
        await rig.client.close()

    async def test_a_recording_and_typed_text_share_the_same_names(self) -> None:
        rig = Rig()
        await rig.connected()

        first = await rig.client.say_audio(RECORDING)
        second = await rig.client.say("and in Italian?")

        assert (first, second) == ("u1", "u2")
        await rig.client.close()

    async def test_a_device_with_no_microphone_cannot_send_a_recording_either(self) -> None:
        rig = Rig("screen")
        await rig.connected()

        with pytest.raises(CannotConverse, match="no microphone"):
            await rig.client.say_audio(RECORDING)

        assert rig.connection.frames("audio_utterance") == []
        await rig.client.close()

    async def test_the_answer_being_given_can_be_stopped_and_nothing_else_can(self) -> None:
        rig = Rig()
        running = await rig.running()
        assert await rig.client.cancel() is False

        identity = await rig.client.say("hello")
        assert await rig.client.cancel() is True
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.CANCELLED))
        await rig.client.wait_for_turn(identity)

        assert [c.id for c in rig.connection.frames("cancel")] == [identity]
        assert rig.client.active is None
        assert await rig.client.cancel() is False
        running.cancel()

    async def test_an_answer_that_never_came_is_not_waited_for_forever_when_the_edge_goes(
        self,
    ) -> None:
        rig = Rig()
        running = await rig.running()
        identity = await rig.client.say("hello")

        rig.connection.drop(1001, "going away")
        ended = await rig.client.wait_for_turn(identity)
        lost = await running

        assert ended is None
        assert lost is not None
        assert (lost.code, lost.reason) == (1001, "going away")
        assert rig.client.active is None

    async def test_an_answer_that_was_never_asked_for_is_not_waited_for(self) -> None:
        rig = Rig()

        assert await rig.client.wait_for_turn("u99") is None


class TestBeingPlayedTo:
    async def test_speech_with_audio_is_played_through_the_voice_output(self) -> None:
        voice = FakeVoiceOutput()
        rig = Rig(voice_output=voice)
        running = await rig.running()
        identity = await rig.client.say("hello")

        rig.connection.say(part(0, Speech(text="Hold the button.", audio=RECORDING)))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)

        assert rig.shown.of("speech") == ["Hold the button."]
        assert voice.played == [RECORDING]
        running.cancel()

    async def test_speech_with_no_audio_is_shown_but_nothing_is_played(self) -> None:
        voice = FakeVoiceOutput()
        rig = Rig(voice_output=voice)
        running = await rig.running()
        identity = await rig.client.say("hello")

        rig.connection.say(part(0, Speech(text="Hold the button.")))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)

        assert voice.played == []
        running.cancel()

    async def test_without_a_voice_output_speech_with_audio_is_still_only_shown(self) -> None:
        rig = Rig()  # no voice_output given
        running = await rig.running()
        identity = await rig.client.say("hello")

        rig.connection.say(part(0, Speech(text="Hold the button.", audio=RECORDING)))
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)

        assert rig.shown.of("speech") == ["Hold the button."]
        running.cancel()


class TestSayingGoodbye:
    async def test_it_says_goodbye_and_closes_and_can_do_it_twice(self) -> None:
        rig = Rig()
        await rig.connected()

        await rig.client.close()
        await rig.client.close()

        assert len(rig.connection.frames("bye")) == 1
        assert rig.connection.closed is True


def test_the_profile_is_the_one_asked_for() -> None:
    profile: Profile = get_profile("player")

    assert Capability.MEDIA_PLAYER in profile.capabilities
