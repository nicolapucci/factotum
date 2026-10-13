"""The timers and the alarms of a device, as a client carries them (ADR 0029): the commands that set
and cancel them and the answers that go back, how the device says how it is, and what a ring does
with an answer that is being given, a person who speaks and a connection that is lost."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from factotum.client_core import CommandRouter, DeviceClient
from factotum.client_core.testing import (
    ClientRig,
    FakeVoiceOutput,
    FakeVoiceSink,
    RecordingCommands,
    settle,
)
from factotum.client_core.timekeeping import RING_EVERY_S
from factotum.contracts.audio import AudioPayload, wav_of
from factotum.contracts.commands import (
    AlarmDay,
    CancelAlarm,
    CancelTimer,
    CommandResult,
    CommandStatus,
    GetState,
    SetAlarm,
    SetTimer,
)
from factotum.contracts.edge import CommandFrame, Feature, TurnEnded, TurnStatus
from factotum.contracts.timekeeping import (
    AlarmsCancelled,
    AlarmSet,
    TimersCancelled,
    TimerSet,
    TimeState,
    answer_in,
)

PLAYING = frozenset({Feature.PLAY_AUDIO})


def clip(fill: int = 1) -> AudioPayload:
    return wav_of(fill.to_bytes(2, "little", signed=True) * 100, 22_050)


def pcm_of_fill(fill: int) -> bytes:
    return fill.to_bytes(2, "little", signed=True) * 100


def timer(seconds: int = 60, text: str = "Timer is up.", fill: int = 1) -> CommandFrame:
    return CommandFrame(
        id=f"c-timer-{seconds}", command=SetTimer(seconds=seconds, text=text, audio=clip(fill))
    )


async def acked(rig: ClientRig, identity: str) -> CommandResult:
    async with asyncio.timeout(2):
        while True:
            for ack in rig.connection.frames("ack"):
                if ack.id == identity:
                    return ack.result  # type: ignore[no-any-return]
            await asyncio.sleep(0)


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110 - a state to watch, there is no event
            await asyncio.sleep(0.001)


async def ask(rig: ClientRig, command: Any, identity: str = "c1") -> CommandResult:
    rig.connection.say(CommandFrame(id=identity, command=command))
    return await acked(rig, identity)


class TestSettingAndCancelling:
    async def test_a_timer_is_set_and_the_device_answers_with_the_one_it_set(self) -> None:
        rig = ClientRig("speaker", voice_output=FakeVoiceOutput())
        running = await rig.running(features=PLAYING)

        result = await ask(rig, SetTimer(seconds=300, text="Done.", audio=clip()))

        assert result.succeeded
        answer = answer_in(TimerSet, result.data)
        assert answer is not None
        assert answer.timer is not None
        assert (answer.timer.seconds, answer.timer.left_s) == (300, 300)
        assert rig.hands.commands == []  # it is not the hands': it is the keeper's
        rig.connection.drop()
        await running

    async def test_a_timer_is_cancelled(self) -> None:
        rig = ClientRig("speaker", voice_output=FakeVoiceOutput())
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=300, text="Done.", audio=clip()), "c1")

        result = await ask(rig, CancelTimer(), "c2")

        assert result.succeeded
        answer = answer_in(TimersCancelled, result.data)
        assert answer is not None
        assert [timer.seconds for timer in answer.cancelled] == [300]
        rig.connection.drop()
        await running

    async def test_an_alarm_is_set_for_the_moment_the_device_works_out_and_is_cancelled(
        self,
    ) -> None:
        rig = ClientRig("speaker", voice_output=FakeVoiceOutput())
        running = await rig.running(features=PLAYING)

        set_ = await ask(rig, SetAlarm(hour=7, minute=0, text="Up.", audio=clip()), "c1")
        gone = await ask(rig, CancelAlarm(hour=7, minute=0), "c2")

        said = answer_in(AlarmSet, set_.data)
        assert said is not None
        assert said.alarm is not None
        assert said.alarm.at == datetime(2026, 10, 14, 7, 0)  # noqa: DTZ001 - civil time
        cancelled = answer_in(AlarmsCancelled, gone.data)
        assert cancelled is not None
        assert [alarm.id for alarm in cancelled.cancelled] == [said.alarm.id]
        rig.connection.drop()
        await running

    async def test_a_day_that_a_person_said_is_the_devices_to_work_out(self) -> None:
        rig = ClientRig("speaker", voice_output=FakeVoiceOutput())
        running = await rig.running(features=PLAYING)

        result = await ask(
            rig, SetAlarm(hour=7, minute=0, day=AlarmDay(in_days=0), text="Up.", audio=clip())
        )

        said = answer_in(AlarmSet, result.data)
        assert said is not None
        assert (said.alarm, said.reason) == (None, "past")  # it is evening: seven is over
        rig.connection.drop()
        await running


class TestHowTheDeviceIs:
    async def test_it_says_what_its_hands_have_and_what_the_keeper_holds(self) -> None:
        hands = RecordingCommands(
            CommandResult.ok({"volume": 40, "muted": False}),
            capabilities=ClientRig("speaker").identity.capabilities,
        )
        rig = ClientRig("speaker", voice_output=FakeVoiceOutput(), hands=hands)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetAlarm(hour=7, minute=0, text="Up.", audio=clip()), "c1")
        await ask(rig, SetTimer(seconds=90, text="Done.", audio=clip()), "c2")

        result = await ask(rig, GetState(), "c3")

        assert result.succeeded
        assert result.data is not None
        assert (result.data["volume"], result.data["muted"]) == (40, False)
        state = answer_in(TimeState, result.data)
        assert state is not None
        assert [timer.seconds for timer in state.timers] == [90]
        assert [alarm.at.hour for alarm in state.alarms] == [7]
        assert state.now == rig.clock.wall()
        assert hands.commands == [GetState()]  # the hands were asked what they have
        rig.connection.drop()
        await running

    async def test_a_device_whose_hands_have_no_volume_says_what_the_keeper_holds(self) -> None:
        hands = RecordingCommands(
            CommandResult(status=CommandStatus.UNSUPPORTED, detail="no volume here"),
            capabilities=ClientRig("speaker").identity.capabilities,
        )
        rig = ClientRig("speaker", voice_output=FakeVoiceOutput(), hands=hands)
        running = await rig.running(features=PLAYING)

        result = await ask(rig, GetState())

        assert result.succeeded
        assert result.data is not None
        assert "volume" not in result.data
        assert answer_in(TimeState, result.data) is not None
        rig.connection.drop()
        await running

    async def test_hands_that_fail_are_not_covered_for(self) -> None:
        failure = CommandResult(status=CommandStatus.FAILED, detail="the amplifier is off")
        hands = RecordingCommands(failure, capabilities=ClientRig("speaker").identity.capabilities)
        rig = ClientRig("speaker", voice_output=FakeVoiceOutput(), hands=hands)
        running = await rig.running(features=PLAYING)

        result = await ask(rig, GetState())

        assert result == failure
        rig.connection.drop()
        await running

    async def test_a_device_that_keeps_no_time_says_only_what_its_hands_have(self) -> None:
        rig = ClientRig("player")  # it has a speaker and no sound port: it does not play clips
        running = await rig.running()

        result = await ask(rig, GetState())

        assert result.succeeded
        assert result.data is None
        assert rig.hands.commands == [GetState()]
        rig.connection.drop()
        await running


class TestADeviceThatKeepsNoTime:
    async def test_it_is_told_so_and_nothing_is_set(self) -> None:
        rig = ClientRig("screen")
        running = await rig.running()

        timer_ = await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip()), "c1")
        alarm = await ask(rig, SetAlarm(hour=7, minute=0, text="Up.", audio=clip()), "c2")

        assert timer_.status is CommandStatus.UNSUPPORTED
        assert alarm.status is CommandStatus.UNSUPPORTED
        assert (
            rig.shown.of("violation")
            == ["the edge set a timer or an alarm on a device that did not say it plays clips"] * 2
        )
        rig.connection.drop()
        await running

    async def test_cancelling_one_it_does_not_have_is_not_the_edges_mistake(self) -> None:
        rig = ClientRig("screen")
        running = await rig.running()

        timer_ = await ask(rig, CancelTimer(), "c1")
        alarm = await ask(rig, CancelAlarm(), "c2")

        assert timer_.status is CommandStatus.UNSUPPORTED
        assert alarm.status is CommandStatus.UNSUPPORTED
        assert rig.shown.of("violation") == []
        rig.connection.drop()
        await running

    async def test_a_speaker_with_nothing_to_play_with_keeps_none_either(self) -> None:
        rig = ClientRig("speaker")  # audio_out, and no sound port: no feature, no clips
        running = await rig.running()

        result = await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip()))

        assert result.status is CommandStatus.UNSUPPORTED
        rig.connection.drop()
        await running


class TestWhenItRings:
    async def test_the_clip_is_said_like_an_announcement_when_the_timer_is_up(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Pasta is ready.", audio=clip(7)))

        await rig.clock.advance(59)
        assert sink.tracks == []
        await rig.clock.advance(1)
        await until(lambda: len(sink.tracks) == 1)
        await rig.client.wait_until_played()

        assert bytes(sink.tracks[0].pcm) == pcm_of_fill(7)
        assert rig.shown.of("event") == [
            "timer t1 set for 60 s",
            "timer t1 is up",
            "announcement: Pasta is ready.",
        ]
        rig.connection.drop()
        await running

    async def test_it_rings_again_until_it_is_cancelled(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Pasta is ready.", audio=clip(7)))

        await rig.clock.advance(60)
        await until(lambda: len(sink.tracks) == 1)
        await rig.clock.advance(RING_EVERY_S + 2)
        await until(lambda: len(sink.tracks) == 2)
        await ask(rig, CancelTimer(), "c2")
        await rig.clock.advance(RING_EVERY_S * 3)
        await settle()

        assert len(sink.tracks) == 2
        rig.connection.drop()
        await running

    async def test_not_while_an_answer_is_being_given_but_when_it_has_been(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip(5)))
        identity = await rig.client.say("tell me something")

        await rig.clock.advance(60)
        await settle()

        assert sink.tracks == []  # the answer is being given: the ring waits
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)
        await until(lambda: len(sink.tracks) == 1)
        assert bytes(sink.tracks[0].pcm) == pcm_of_fill(5)
        rig.connection.drop()
        await running

    async def test_a_person_who_speaks_cuts_it_and_it_comes_back_when_they_are_done(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip(5)))
        await rig.clock.advance(60)
        await asyncio.wait_for(sink.writing.wait(), 2)  # the ring is being said

        identity = await rig.client.say("stop")  # a person speaks over it

        assert "abort" in sink.calls
        gate.set()
        rig.connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))
        await rig.client.wait_for_turn(identity)
        await rig.clock.advance(RING_EVERY_S + 2)
        await until(lambda: len(sink.tracks) == 2)  # it was not stopped by being spoken over
        rig.connection.drop()
        await running

    async def test_cancelling_it_cuts_the_ring_that_is_being_said(self) -> None:
        gate = asyncio.Event()
        sink = FakeVoiceSink(gate=gate)
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip(5)))
        await rig.clock.advance(60)
        await asyncio.wait_for(sink.writing.wait(), 2)

        await ask(rig, CancelTimer(), "c2")
        await settle()

        assert sink.tracks[0].aborted  # it stopped on the spot, and was not left to end
        gate.set()
        await rig.client.wait_until_played()
        assert not rig.client.playing
        await rig.clock.advance(RING_EVERY_S * 3)
        await settle()
        assert len(sink.tracks) == 1
        rig.connection.drop()
        await running

    async def test_cancelling_one_that_waits_to_be_said_takes_it_off_the_queue(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip(5)))
        await rig.client.say("tell me something")  # an answer is being given: the ring waits
        await rig.clock.advance(60)
        await settle()
        assert rig.client.playing  # something waits to be said

        await ask(rig, CancelTimer(), "c2")
        await settle()

        assert not rig.client.playing
        assert sink.tracks == []
        rig.connection.drop()
        await running

    async def test_a_ring_that_is_not_a_clip_is_said_to_be_one_and_the_timer_goes_on(self) -> None:
        sink = FakeVoiceSink()
        rig = ClientRig("speaker", voice_sink=sink)
        running = await rig.running(features=PLAYING)
        not_a_clip = AudioPayload(sample_rate=22_050, data=b"this is not a WAV file")
        await ask(rig, SetTimer(seconds=60, text="Done.", audio=not_a_clip))

        await rig.clock.advance(60)
        await settle()

        assert sink.tracks == []
        [notice] = rig.shown.of("notice")[-1:]
        assert notice.startswith("a ring is not a clip that can be played")
        rig.connection.drop()
        await running


class TestWhenTheConnectionIsLost:
    async def test_a_ring_that_waited_to_be_said_is_given_up_on_and_the_next_is_on_the_speaker(
        self,
    ) -> None:
        output = FakeVoiceOutput()
        rig = ClientRig("speaker", voice_output=output)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip(4)))
        await rig.client.say("tell me something")  # an answer is being given: the ring waits
        await rig.clock.advance(60)
        await settle()
        assert rig.client.playing
        assert output.played == []

        rig.connection.drop()  # and the connection is lost before the ring was said
        await running
        await rig.clock.advance(RING_EVERY_S + 2)
        await until(lambda: len(output.played) == 1)

        assert output.played[0].data == clip(4).data
        await rig.timekeeper.close()

    async def test_the_timer_goes_on_and_rings_on_the_speaker_itself(self) -> None:
        output = FakeVoiceOutput()
        rig = ClientRig("speaker", voice_output=output)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip(4)))

        rig.connection.drop()  # the edge is gone
        await running
        await rig.clock.advance(60)
        await until(lambda: len(output.played) == 1)

        assert output.played[0].data == clip(4).data
        assert "announcement: Done." not in rig.shown.of("event")  # no client said it
        await rig.timekeeper.close()

    async def test_the_next_connection_takes_over_the_ringing(self) -> None:
        output = FakeVoiceOutput()
        rig = ClientRig("speaker", voice_output=output)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip(4)))
        rig.connection.drop()
        await running
        await rig.clock.advance(60)
        await until(lambda: len(output.played) == 1)

        sink = FakeVoiceSink()
        again = ClientRig("speaker", voice_sink=sink, timekeeper=rig.timekeeper)  # the same device
        await again.running(features=PLAYING)
        await rig.clock.advance(RING_EVERY_S + 2)
        await until(lambda: len(sink.tracks) == 1)

        assert bytes(sink.tracks[0].pcm) == pcm_of_fill(4)
        assert len(output.played) == 1  # and not on the speaker as well
        assert "announcement: Done." in again.shown.of("event")  # the client that is there said it
        assert "announcement: Done." not in rig.shown.of("event")  # and not the one that went
        await again.client.close()
        await rig.timekeeper.close()


class TestWhoseKeeperItIs:
    async def test_a_keeper_that_is_the_devices_is_left_alone_when_a_client_is_closed(self) -> None:
        output = FakeVoiceOutput()
        rig = ClientRig("speaker", voice_output=output)
        running = await rig.running(features=PLAYING)
        await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip(4)))

        await rig.client.close()  # the connection ends, and the device does not
        rig.connection.drop()
        await running

        held = answer_in(TimeState, rig.timekeeper.state())
        assert held is not None
        assert len(held.timers) == 1
        await rig.clock.advance(60)
        await until(lambda: len(output.played) == 1)
        await rig.timekeeper.close()

    async def test_a_client_that_was_given_none_keeps_its_own_and_closing_it_forgets_it(
        self,
    ) -> None:
        rig = ClientRig("speaker")
        client = DeviceClient(
            rig.connection,
            rig.identity,
            rig.shown,
            CommandRouter(rig.hands),
            voice_sink=FakeVoiceSink(),
        )
        rig.connection.say(rig.ready(features=PLAYING))
        await client.connect()
        running = asyncio.create_task(client.run())

        result = await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip()))

        assert answer_in(TimerSet, result.data) is not None
        assert _keeping_time()  # it is looking at the clock
        await client.close()
        rig.connection.drop()
        await running
        assert not _keeping_time()  # and it is not any more

    async def test_a_screen_has_none_to_keep(self) -> None:
        rig = ClientRig("screen")
        client = DeviceClient(rig.connection, rig.identity, rig.shown, CommandRouter(rig.hands))
        rig.connection.say(rig.ready())
        await client.connect()
        running = asyncio.create_task(client.run())

        result = await ask(rig, SetTimer(seconds=60, text="Done.", audio=clip()))

        assert result.status is CommandStatus.UNSUPPORTED
        assert not _keeping_time()
        await client.close()
        rig.connection.drop()
        await running


def _keeping_time() -> bool:
    return any(task.get_name() == "timekeeper" and not task.done() for task in asyncio.all_tasks())
