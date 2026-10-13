"""A text that a skill has a device say (ADR 0026): the edge makes the voice of it, a sentence at a
time, and sends it as clips that a device which said it plays takes one after the other; to a device
that did not say so a text is sent as words, and a clip as the words that it carries."""

import asyncio
from typing import Any

from factotum.contracts.audio import AudioPayload, wav_of
from factotum.contracts.capabilities import Capability
from factotum.contracts.commands import (
    CancelAlarm,
    CancelTimer,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    GetState,
    PlayAudio,
    SetAlarm,
    SetTimer,
    SetVolume,
    Speak,
)
from factotum.contracts.edge import Ack, Feature
from factotum.contracts.errors import GatewayUnavailableError, TtsNotConfiguredError
from factotum.contracts.identity import Owner, Subject
from factotum.edge.session import Session
from factotum.edge.testing import (
    TENANT,
    USER,
    Connected,
    FakeSpeech,
    Rig,
    make_device,
)

PLAYS = (Feature.PLAY_AUDIO,)
TWO_SENTENCES = "Boil the water with plenty of salt. Then add the pasta and wait ten minutes."


def clip(fill: int) -> AudioPayload:
    return wav_of(fill.to_bytes(2, "little", signed=True) * 50, 22_050)


class GatedSpeech(FakeSpeech):
    """A voice that makes the sentences it is told to wait for only when a test lets it."""

    def __init__(self) -> None:
        super().__init__()
        self.gates: dict[int, asyncio.Event] = {}
        self.begun: list[str] = []

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
        self.begun.append(text)
        gate = self.gates.get(len(self.begun))
        if gate is not None:
            await gate.wait()
        return await super().synthesize(owner, text)


def said(session: Session, text: str) -> "asyncio.Task[CommandResult]":
    return asyncio.create_task(session.command(Speak(text=text)))


async def accepting(connected: Connected, count: int, result: CommandResult | None = None) -> Any:
    """The device takes the next ``count`` commands, one after the other, and says it did."""
    taken: list[Any] = []
    for number in range(1, count + 1):
        frames = await connected.wire.wait_for("command", count=number)
        taken.append(frames[number - 1])
        connected.wire.say(Ack(id=frames[number - 1].id, result=result or CommandResult.ok()))
    return taken


class TestASpeakThatIsMadeHere:
    async def test_each_sentence_is_made_and_sent_as_a_clip_in_order(self) -> None:
        speech = FakeSpeech().will_synthesize(clip(1), clip(2))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)

            asked = said(connected.session, TWO_SENTENCES)
            sent = await accepting(connected, 2)

            assert await asked == CommandResult.ok()
            assert [command.command for command in sent] == [
                PlayAudio(text="Boil the water with plenty of salt.", audio=clip(1)),
                PlayAudio(text="Then add the pasta and wait ten minutes.", audio=clip(2)),
            ]
            assert [text for _, text in speech.syntheses] == [
                "Boil the water with plenty of salt.",
                "Then add the pasta and wait ten minutes.",
            ]

    async def test_the_first_sentence_is_sent_while_the_next_is_still_being_made(self) -> None:
        speech = GatedSpeech()
        speech.gates[2] = asyncio.Event()  # the second sentence is not made until the test says
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)

            asked = said(connected.session, TWO_SENTENCES)
            [first] = await connected.wire.wait_for("command")

            assert isinstance(first.command, PlayAudio)
            assert first.command.text == "Boil the water with plenty of salt."
            assert not asked.done()  # nothing is done until the whole text has been taken
            connected.wire.say(Ack(id=first.id, result=CommandResult.ok()))
            speech.gates[2].set()
            [_, second] = await connected.wire.wait_for("command", count=2)
            connected.wire.say(Ack(id=second.id, result=CommandResult.ok()))
            assert await asked == CommandResult.ok()

    async def test_it_is_made_with_the_voice_of_whom_the_device_acts_for(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)

            asked = said(connected.session, "Dinner is ready.")
            await accepting(connected, 1)
            await asked

            [(owner, _)] = speech.syntheses
            assert owner == Owner.of_subject(
                Subject.tenant(TENANT)
            )  # a shared device is the tenant's

    async def test_a_persons_own_device_is_made_a_voice_with_theirs(self) -> None:
        speech = FakeSpeech()
        phone = make_device("My phone", "phone-01", shared=False)
        async with Rig(phone, speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)

            asked = said(connected.session, "Dinner is ready.")
            await accepting(connected, 1)
            await asked

            [(owner, _)] = speech.syntheses
            assert owner == Owner.of_subject(Subject.user(USER))

    async def test_what_is_for_the_eyes_is_not_said(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)

            asked = said(connected.session, "**Dinner** is [ready](https://x.example) [1].")
            [sent] = await accepting(connected, 1)
            await asked

            assert sent.command.text == "Dinner is ready."

    async def test_a_text_with_nothing_to_say_is_not_a_command_done(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect(features=PLAYS)

            result = await connected.session.command(Speak(text="**"))

            assert (result.status, result.detail) == (
                CommandStatus.FAILED,
                "there is nothing to say",
            )
            assert connected.wire.frames("command") == []

    async def test_two_texts_at_once_are_said_one_after_the_other(self) -> None:
        speech = GatedSpeech()
        speech.gates[1] = asyncio.Event()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)

            one = said(connected.session, "First text, which is a sentence.")
            await asyncio.sleep(0.01)
            two = said(connected.session, "Second text, which is another.")
            await asyncio.sleep(0.01)
            speech.gates[1].set()
            sent = await accepting(connected, 2)

            assert [command.command.text for command in sent] == [
                "First text, which is a sentence.",
                "Second text, which is another.",
            ]
            assert (await one, await two) == (CommandResult.ok(), CommandResult.ok())


class TestWhenItCannotBeSaid:
    async def test_an_edge_with_no_voice_says_so_and_sends_nothing(self) -> None:
        async with Rig() as rig:  # no speech
            connected = await rig.connect(features=PLAYS)

            result = await connected.session.command(Speak(text="Dinner is ready."))

            assert result.status is CommandStatus.FAILED
            assert result.detail == "the edge has no voice to say it with"
            assert connected.wire.frames("command") == []

    async def test_a_voice_that_is_not_bound_is_a_voice_that_could_not_be_made(self) -> None:
        speech = FakeSpeech().will_synthesize(TtsNotConfiguredError("no voice is bound"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)

            result = await connected.session.command(Speak(text="Dinner is ready."))

            assert result.status is CommandStatus.FAILED
            assert result.detail is not None
            assert "tts_not_configured" in result.detail
            assert connected.wire.frames("command") == []

    async def test_a_voice_that_breaks_in_the_middle_stops_where_it_was(self) -> None:
        speech = FakeSpeech().will_synthesize(clip(1), GatewayUnavailableError("it is away"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)

            asked = said(connected.session, TWO_SENTENCES)
            await accepting(connected, 1)
            result = await asked

            assert result.status is CommandStatus.FAILED
            assert (
                len(connected.wire.frames("command")) == 1
            )  # the first was sent, the rest was not

    async def test_a_voice_that_fails_in_a_way_nobody_planned_is_a_voice_that_could_not_be_made(
        self,
    ) -> None:
        speech = FakeSpeech().will_synthesize(RuntimeError("a bug"))
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)

            result = await connected.session.command(Speak(text="Dinner is ready."))

            assert (result.status, result.detail) == (
                CommandStatus.FAILED,
                "the voice could not be made",
            )

    async def test_a_device_that_does_not_take_a_sentence_ends_it_there(self) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect(features=PLAYS)
            refused = CommandResult(status=CommandStatus.FAILED, detail="too much is waiting")

            asked = said(connected.session, TWO_SENTENCES)
            await accepting(connected, 1, refused)

            assert await asked == refused
            assert len(speech.syntheses) == 1  # the second was not even made

    async def test_a_device_that_goes_while_it_is_told_is_offline(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect(features=PLAYS)

            asked = said(connected.session, TWO_SENTENCES)
            await connected.wire.wait_for("command")
            connected.wire.hang_up()

            assert (await asked).status is CommandStatus.OFFLINE


class TestAClipAndAFeature:
    async def test_a_clip_goes_to_a_device_that_plays_it_as_it_is(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect(features=PLAYS)
            command = PlayAudio(text="Dinner is ready.", audio=clip(5))

            asked = asyncio.create_task(connected.session.command(command))
            [sent] = await accepting(connected, 1)

            assert await asked == CommandResult.ok()
            assert sent.command == command

    async def test_a_clip_that_a_device_cannot_play_is_sent_as_the_words_it_carries(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()  # a device that did not say it plays clips

            asked = asyncio.create_task(
                connected.session.command(PlayAudio(text="Dinner is ready.", audio=clip(5)))
            )
            [sent] = await accepting(connected, 1)

            assert await asked == CommandResult.ok()
            assert sent.command == Speak(text="Dinner is ready.")

    async def test_a_text_to_a_device_that_does_not_play_clips_is_sent_as_it_always_was(
        self,
    ) -> None:
        speech = FakeSpeech()
        async with Rig(speech=speech) as rig:
            connected = await rig.connect()

            asked = said(connected.session, TWO_SENTENCES)
            [sent] = await accepting(connected, 1)

            assert await asked == CommandResult.ok()
            assert sent.command == Speak(text=TWO_SENTENCES)
            assert speech.syntheses == []  # no voice was made: the device has the words

    async def test_a_feature_is_not_taken_by_a_device_that_may_not_have_it(self) -> None:
        # What a device declares is what the edge takes of it, never more than it is registered for:
        # this one is registered for a microphone, and plays nothing.
        deaf = make_device(capabilities=(Capability.AUDIO_IN,))
        async with Rig(deaf) as rig:
            connected = await rig.connect(
                capabilities=(Capability.AUDIO_IN, Capability.AUDIO_OUT), features=PLAYS
            )

            assert connected.ready.features == frozenset()
            assert connected.session.features == frozenset()

    async def test_a_device_that_is_asked_for_a_volume_still_gets_it_as_a_command(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect(features=PLAYS)

            asked = asyncio.create_task(connected.session.command(SetVolume(level=30)))
            [sent] = await accepting(connected, 1)

            assert await asked == CommandResult.ok()
            assert sent.command == SetVolume(level=30)


class TestARingAndAFeature:
    """A timer or an alarm rings with a clip (ADR 0029), and there is no way to say it in words."""

    async def test_it_goes_to_a_device_that_plays_clips_as_it_is(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect(features=PLAYS)
            commands: list[DeviceCommand] = [
                SetTimer(seconds=60, text="Done.", audio=clip(5)),
                SetAlarm(hour=7, minute=0, text="Up.", audio=clip(6)),
            ]

            for number, command in enumerate(commands, 1):
                asked = asyncio.create_task(connected.session.command(command))
                [*_, sent] = await accepting(connected, number)
                assert await asked == CommandResult.ok()
                assert sent.command == command

    async def test_it_does_not_go_to_a_device_that_did_not_say_it_plays_clips(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()  # no feature: the device has no clip to ring with

            rings: list[DeviceCommand] = [
                SetTimer(seconds=60, text="Done.", audio=clip(5)),
                SetAlarm(hour=7, minute=0, text="Up.", audio=clip(6)),
            ]
            for command in rings:
                result = await connected.session.command(command)

                assert result.status is CommandStatus.UNSUPPORTED
                assert result.detail == "the device did not say it plays clips, so it cannot ring"
            assert connected.wire.frames("command") == []  # nothing was sent

    async def test_what_stops_a_ring_or_asks_about_one_goes_to_any_device(self) -> None:
        async with Rig(speech=FakeSpeech()) as rig:
            connected = await rig.connect()

            others: list[DeviceCommand] = [CancelTimer(), CancelAlarm(), GetState()]
            for number, command in enumerate(others, 1):
                asked = asyncio.create_task(connected.session.command(command))
                [*_, sent] = await accepting(connected, number)

                assert await asked == CommandResult.ok()
                assert sent.command == command
