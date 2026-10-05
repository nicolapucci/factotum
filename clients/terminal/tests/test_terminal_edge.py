"""The terminal against a real edge: uvicorn serving the edge on a socket, the WebSocket library of
the client on the other side, and a conversation service that answers as told. What the two say to
each other is what a device and an edge would say, so this is where the two halves of the protocol
are shown to fit. (The tests of the edge alone are in ``services/edge``, those of the client alone
are the other files here.)"""

import asyncio
import io
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from factotum.client_core import (
    AudioStream,
    ConnectionLost,
    DeviceClient,
    Refused,
    WebSocketConnection,
)
from factotum.client_core.testing import RecordingRenderer, never
from factotum.common.devauth import DEVICE_HEADER
from factotum.contracts.audio import pcm_of, wav_of
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import CommandStatus, ShowCard
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.edge import CloseCode, CommandRequest, TurnStatus
from factotum.contracts.speech import SynthesisFormat, SynthesisSamples
from factotum.edge.testing import (
    HANG,
    FakeSpeech,
    Rig,
    Serving,
    answer,
    citation,
    forgotten,
    make_device,
    serving,
)
from factotum.terminal.cli import Options, run
from factotum.terminal.device import TerminalDevice
from factotum.terminal.profiles import PROFILES, get_profile
from factotum.terminal.testing import Typed

ANSWER = "Hold the button for ten seconds [1]. Then let go."


def registered(*names: str) -> list[tuple[SystemDeviceRecord, str]]:
    """The devices of these profiles, registered as the example file has them."""
    profiles = [get_profile(name) for name in names]
    return [
        make_device(
            name=profile.name,
            hardware_id=profile.hardware_id,
            token=profile.token,
            capabilities=profile.capabilities,
            display=profile.display,
            shared=profile.name != "phone",
        )
        for profile in profiles
    ]


@dataclass
class Terminal:
    client: DeviceClient
    device: TerminalDevice
    shown: RecordingRenderer
    listening: asyncio.Task[ConnectionLost | None]


@asynccontextmanager
async def a_terminal(
    edge: Serving, profile: str, *, token: str | None = None
) -> AsyncIterator[Terminal]:
    """A device of this profile, connected to the edge and told it is ready."""
    chosen = get_profile(profile)
    shown = RecordingRenderer()
    device = TerminalDevice(chosen.capabilities, shown)
    client = DeviceClient(
        await WebSocketConnection.open(edge.websocket_url), chosen, shown, device, token=token
    )
    try:
        await client.connect()
    except BaseException:
        await client.close()
        raise
    listening = asyncio.create_task(client.run())
    try:
        yield Terminal(client, device, shown, listening)
    finally:
        await client.close()
        await asyncio.wait({listening})


async def until(condition: Callable[[], bool], timeout_s: float = 3.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():  # noqa: ASYNC110 - a state to watch, there is no event
            await asyncio.sleep(0.005)


async def asked(terminal: Terminal, text: str) -> None:
    """Something said to the assistant, and its answer to the end."""
    identity = await terminal.client.say(text)
    ended = await asyncio.wait_for(terminal.client.wait_for_turn(identity), 5)
    assert ended is not None
    assert ended.status is TurnStatus.FINISHED, ended


class TestAnAnswerAsEachDeviceGetsIt:
    async def test_a_speaker_says_the_answer_without_its_references_and_shows_nothing(self) -> None:
        async with Rig(*registered("speaker")) as rig, serving(rig) as edge:
            rig.conversation.scripts.append(answer(ANSWER, sources=[citation()]))
            async with a_terminal(edge, "speaker") as speaker:
                await asked(speaker, "how do I reset the router?")

                assert " ".join(speaker.shown.of("speech")) == (
                    "Hold the button for ten seconds. Then let go."
                )
                assert speaker.shown.of("text") == []
                assert speaker.shown.of("card") == []
                assert speaker.shown.of("violation") == []
            assert await forgotten(rig)

    async def test_a_smart_display_shows_the_answer_and_where_it_comes_from_and_says_it_too(
        self,
    ) -> None:
        async with Rig(*registered("smart-display")) as rig, serving(rig) as edge:
            rig.conversation.scripts.append(
                answer(ANSWER, sources=[citation(title="Router manual")])
            )
            async with a_terminal(edge, "smart-display") as display:
                await asked(display, "how do I reset the router?")

                # each sentence is a part of its own: what shows them puts them one after another
                assert " ".join(display.shown.of("text")) == ANSWER
                assert " ".join(display.shown.of("speech")) == (
                    "Hold the button for ten seconds. Then let go."
                )
                [card] = display.shown.of("card")
                assert card.title == "Sources"
                assert card.items[0].label == "[1] Router manual"
                assert display.shown.of("violation") == []
            assert await forgotten(rig)

    async def test_what_was_said_is_what_the_conversation_service_was_asked_for_the_device(
        self,
    ) -> None:
        async with Rig(*registered("speaker")) as rig, serving(rig) as edge:
            async with a_terminal(edge, "speaker") as speaker:
                await asked(speaker, "what time is it in Rome?")

            [(principal, message)] = rig.conversation.turns
            assert message == "what time is it in Rome?"
            assert principal.device_id is not None

    async def test_speaking_over_an_answer_interrupts_it_and_the_edge_stops_the_model(self) -> None:
        async with Rig(*registered("speaker")) as rig, serving(rig) as edge:
            rig.conversation.scripts.append(
                [*answer("Once upon a time there was a very long story. And it went on")[:-1], HANG]
            )
            rig.conversation.scripts.append(answer("Right, another time."))
            async with a_terminal(edge, "speaker") as speaker:
                first = await speaker.client.say("tell me a story")
                await until(lambda: bool(speaker.shown.of("speech")))
                second = await speaker.client.say("never mind")

                cut = await asyncio.wait_for(speaker.client.wait_for_turn(first), 5)
                done = await asyncio.wait_for(speaker.client.wait_for_turn(second), 5)

                assert cut is not None
                assert done is not None
                assert (cut.status, done.status) == (TurnStatus.CANCELLED, TurnStatus.FINISHED)
                assert speaker.shown.of("speech")[-1] == "Right, another time."
                assert speaker.shown.of("violation") == []
            assert await forgotten(rig)
            assert rig.conversation.abandoned == 1


PERSON = b"\x01\x00" * 1600
"""A tenth of a second of somebody talking, for what the fake Whisper of the edge is given."""


class TestSpeakingAsItIsHeard:
    async def test_what_is_said_as_a_stream_is_heard_shown_and_answered(self) -> None:
        speech = FakeSpeech().will_transcribe("how do I reset the router?")
        async with Rig(*registered("speaker"), speech=speech) as rig, serving(rig) as edge:
            rig.conversation.scripts.append(answer(ANSWER, sources=[citation()]))
            async with a_terminal(edge, "speaker") as speaker:
                identity = await speaker.client.say_audio_stream(AudioStream.of(PERSON * 5, 16000))
                ended = await asyncio.wait_for(speaker.client.wait_for_turn(identity), 5)

                assert ended is not None
                assert ended.status is TurnStatus.FINISHED, ended
                assert speaker.shown.of("heard") == ["how do I reset the router?"]
                assert " ".join(speaker.shown.of("speech")) == (
                    "Hold the button for ten seconds. Then let go."
                )
                assert speaker.shown.of("violation") == []
            [(_, recording)] = speech.transcriptions
            assert pcm_of(recording.data) == (PERSON * 5, 16000)
            assert [text for _, text in rig.conversation.turns] == ["how do I reset the router?"]
            assert await forgotten(rig)

    async def test_a_person_who_starts_to_talk_stops_the_answer_while_they_are_still_talking(
        self,
    ) -> None:
        async with Rig(*registered("speaker"), speech=FakeSpeech()) as rig, serving(rig) as edge:
            rig.conversation.scripts.append(
                [*answer("Once upon a time there was a very long story. And it went on")[:-1], HANG]
            )
            rig.conversation.scripts.append(answer("Right, another time."))
            gate = asyncio.Event()

            async def a_person_who_takes_a_while() -> AsyncGenerator[bytes]:
                yield PERSON
                await gate.wait()
                yield PERSON

            async with a_terminal(edge, "speaker") as speaker:
                first = await speaker.client.say("tell me a story")
                await until(lambda: bool(speaker.shown.of("speech")))  # it is being answered
                saying = asyncio.create_task(
                    speaker.client.say_audio_stream(
                        AudioStream(16000, a_person_who_takes_a_while())
                    )
                )

                cut = await asyncio.wait_for(speaker.client.wait_for_turn(first), 5)

                # The answer stopped with the person still in the middle of what they were saying.
                assert cut is not None
                assert cut.status is TurnStatus.CANCELLED
                assert not saying.done()
                assert rig.conversation.abandoned == 1
                gate.set()
                second = await asyncio.wait_for(saying, 5)
                done = await asyncio.wait_for(speaker.client.wait_for_turn(second), 5)
                assert done is not None
                assert done.status is TurnStatus.FINISHED
                assert speaker.shown.of("speech")[-1] == "Right, another time."
                assert speaker.shown.of("violation") == []
            assert await forgotten(rig)

    async def test_an_edge_that_cannot_hear_is_not_talked_to_for_long_and_is_not_waited_for(
        self,
    ) -> None:
        async with Rig(*registered("speaker")) as rig, serving(rig) as edge:  # no speech
            async with a_terminal(edge, "speaker") as speaker:
                identity = await speaker.client.say_audio_stream(
                    AudioStream.of(PERSON * 10, 16000, realtime=True)  # a second of it
                )
                ended = await asyncio.wait_for(speaker.client.wait_for_turn(identity), 5)

                assert ended is not None
                assert (ended.status, ended.code) == (TurnStatus.FAILED, "speech_not_configured")
                errors = [e.code for e in speaker.shown.of("error")]
                assert errors[0] == "speech_not_configured"
                assert errors.count("unknown_stream") <= 2  # it stopped when it was told
                await asked(speaker, "still there?")  # and it is still connected
            assert await forgotten(rig)


class TestTheCommandLineWithFiles:
    async def test_a_recording_in_a_file_is_heard_and_what_is_said_to_it_is_written_to_another(
        self, tmp_path: Path
    ) -> None:
        question = tmp_path / "question.wav"
        question.write_bytes(wav_of(PERSON * 5, 16000).data)
        answer_file = tmp_path / "answer.wav"
        sound = (SynthesisFormat(sample_rate=22050), SynthesisSamples(data=b"\x05\x00" * 100))
        speech = (
            FakeSpeech()
            .will_transcribe("how do I reset the router?")
            .will_stream(*sound)
            .will_stream(*sound)
        )
        async with Rig(*registered("speaker"), speech=speech) as rig, serving(rig) as edge:
            rig.conversation.scripts.append(answer(ANSWER, sources=[citation()]))
            out = io.StringIO()

            code = await run(
                Options(
                    url=edge.websocket_url,
                    profile=get_profile("speaker"),
                    hardware_id=None,
                    token=None,
                    wav_in=(question,),
                    wav_out=answer_file,
                ),
                out=out,
            )

            assert code == 0
            lines = out.getvalue().splitlines()
            assert "[heard]  how do I reset the router?" in lines
            assert lines[-3:-1] == [
                "[say]    Hold the button for ten seconds.",
                "[say]    Then let go.",
            ]
            assert lines[-1].startswith("--       wrote ")
            with wave.open(str(answer_file), "rb") as wav_reader:
                assert wav_reader.getframerate() == 22050
                assert wav_reader.readframes(wav_reader.getnframes()) == b"\x05\x00" * 200
            [(_, recording)] = speech.transcriptions
            assert pcm_of(recording.data) == (PERSON * 5, 16000)
            assert await forgotten(rig)


class TestTheHubReachingADevice:
    async def test_a_card_sent_over_http_is_shown_by_the_screen_that_says_it_did(self) -> None:
        async with Rig(*registered("screen")) as rig, serving(rig) as edge:
            async with (
                a_terminal(edge, "screen") as screen,
                httpx.AsyncClient(base_url=edge.http_url) as http,
            ):
                assert screen.client.ready is not None
                response = await http.post(
                    f"/v1/devices/{screen.client.ready.device_id}/commands",
                    content=CommandRequest(
                        command=ShowCard(card=Card(title="Dinner is ready"))
                    ).model_dump_json(),
                    headers={
                        "content-type": "application/json",
                        DEVICE_HEADER: str(screen.client.ready.device_id),
                    },
                )

                assert (response.status_code, response.json()["status"]) == (
                    200,
                    CommandStatus.OK.value,
                )
                [card] = screen.shown.of("card")
                assert card.title == "Dinner is ready"
                assert screen.device.state.card == card
            assert await forgotten(rig)


class TestAConnectionThatDoesNotWork:
    async def test_a_device_with_the_wrong_credential_is_refused_and_told_why(self) -> None:
        async with Rig(*registered("speaker")) as rig, serving(rig) as edge:
            chosen = get_profile("speaker")
            client = DeviceClient(
                await WebSocketConnection.open(edge.websocket_url),
                chosen,
                RecordingRenderer(),
                TerminalDevice(chosen.capabilities, RecordingRenderer()),
                token="not-the-credential",
            )

            with pytest.raises(Refused) as refused:
                await client.connect()
            await client.close()

            assert refused.value.code == "auth_failed"
            assert await forgotten(rig)

    async def test_what_a_device_declares_beyond_what_it_is_registered_for_is_said(self) -> None:
        speaker = get_profile("speaker")
        deaf = make_device(
            name="speaker",
            hardware_id=speaker.hardware_id,
            token=speaker.token,
            capabilities=speaker.capabilities - {Capability.AUDIO_IN},
        )
        async with Rig(deaf) as rig, serving(rig) as edge:
            async with a_terminal(edge, "speaker") as terminal:
                assert any(
                    "not registered for audio_in" in what for what in terminal.shown.of("notice")
                )
            assert await forgotten(rig)

    async def test_a_device_that_comes_back_replaces_the_one_it_was_and_the_old_one_is_told(
        self,
    ) -> None:
        async with Rig(*registered("speaker")) as rig, serving(rig) as edge:
            async with a_terminal(edge, "speaker") as before, a_terminal(edge, "speaker") as after:
                lost = await asyncio.wait_for(before.listening, 5)

                assert lost is not None
                assert lost.code == CloseCode.REPLACED
                await asked(after, "still there?")
            assert await forgotten(rig)


class TestTheCommandLineOnARealConnection:
    async def test_a_person_types_and_the_answer_comes_out_as_the_speaker_would(self) -> None:
        async with Rig(*registered("speaker")) as rig, serving(rig) as edge:
            rig.conversation.scripts.append(answer(ANSWER, sources=[citation()]))
            out = io.StringIO()

            code = await run(
                Options(
                    url=edge.websocket_url,
                    profile=get_profile("speaker"),
                    hardware_id=None,
                    token=None,
                ),
                lines=Typed("how do I reset the router?").read(),
                out=out,
            )

            assert code == 0
            assert out.getvalue().splitlines()[-2:] == [
                "[say]    Hold the button for ten seconds.",
                "[say]    Then let go.",
            ]
            assert await forgotten(rig)

    async def test_an_edge_that_goes_away_is_noticed_without_waiting_for_anything_to_be_typed(
        self,
    ) -> None:
        async with Rig(*registered("speaker")) as rig, serving(rig) as edge:
            out = io.StringIO()

            async def going_away() -> None:
                await until(lambda: len(rig.sessions) == 1)
                await rig.sessions.close_all()

            stopping = asyncio.create_task(going_away())
            code = await run(
                Options(
                    url=edge.websocket_url,
                    profile=get_profile("speaker"),
                    hardware_id=None,
                    token=None,
                ),
                lines=Typed(never).read(),
                out=out,
            )
            await stopping

            assert code == 1
            assert (
                out.getvalue()
                .splitlines()[-1]
                .startswith(f"--       the connection to the edge ended ({CloseCode.GOING_AWAY}")
            )
            assert await forgotten(rig)

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("ws://127.0.0.1:9/v1/connect", id="nobody-there"),
            pytest.param("http://127.0.0.1:8084/v1/connect", id="not-a-websocket-address"),
            pytest.param("not an address", id="not-an-address"),
        ],
    )
    async def test_an_edge_that_cannot_be_reached_is_said_and_it_is_a_failure(
        self, url: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await run(
            Options(url=url, profile=get_profile("speaker"), hardware_id=None, token=None),
            lines=Typed().read(),
            out=io.StringIO(),
        )

        assert code == 1
        assert f"could not connect to {url}" in capsys.readouterr().err


def test_every_profile_is_covered_by_a_device_the_edge_can_register() -> None:
    assert [device[0].hardware_id for device in registered(*PROFILES)] == [
        profile.hardware_id for profile in PROFILES.values()
    ]
