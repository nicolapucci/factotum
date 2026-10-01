import json
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import AnyHttpUrl, ValidationError

from factotum.contracts.audio import AudioFormat, AudioPayload
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.cards import Card, CardItem
from factotum.contracts.commands import (
    ClearDisplay,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    LedPattern,
    Pause,
    PlayUrl,
    Resume,
    SetLed,
    SetVolume,
    ShowCard,
    Speak,
    Stop,
)
from factotum.contracts.conversation import MAX_MESSAGE_CHARS
from factotum.contracts.edge import (
    HEARTBEAT_SECONDS,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    Ack,
    AudioUtterance,
    Bye,
    Cancel,
    ClientInfo,
    CloseCode,
    CommandFrame,
    ErrorFrame,
    Hello,
    PartFrame,
    Ping,
    Pong,
    Ready,
    TurnEnded,
    TurnStatus,
    Utterance,
    decode_device_frame,
    decode_edge_frame,
    encode_frame,
)
from factotum.contracts.errors import InvalidFrameError
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

SPEAKER = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT})


def hello(**changes: Any) -> Hello:
    fields: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "hardware_id": "kitchen-01",
        "token": "a-credential",
        "capabilities": SPEAKER,
    }
    return Hello(**{**fields, **changes})


def ready(**changes: Any) -> Ready:
    fields: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": "s1",
        "device_id": DeviceId(uuid4()),
        "name": "Kitchen speaker",
        "capabilities": SPEAKER,
    }
    return Ready(**{**fields, **changes})


def audio_payload(**changes: Any) -> AudioPayload:
    fields: dict[str, Any] = {"sample_rate": 16000, "data": b"\x00\x01\xfe\xff RIFF not really"}
    return AudioPayload(**{**fields, **changes})


DEVICE_FRAMES = [
    hello(),
    hello(
        capabilities=SPEAKER | {Capability.DISPLAY},
        display=DisplayInfo(width=800, height=480, touch=True),
        client=ClientInfo(name="factotum-terminal", version="0.0.0"),
    ),
    Utterance(id="u1", text="how do I reset the router?"),
    AudioUtterance(id="u2", audio=audio_payload()),
    Cancel(id="u1"),
    Ack(id="c1", result=CommandResult.ok()),
    Ack(id="c2", result=CommandResult(status=CommandStatus.FAILED, detail="the amplifier is off")),
    Ping(),
    Bye(),
]

CARD = Card(
    title="Sources", body="Two documents", items=(CardItem(label="[1] Manual", value="p. 4"),)
)

COMMANDS: tuple[DeviceCommand, ...] = (
    Speak(text="Dinner is ready", language="en"),
    PlayUrl(url=AnyHttpUrl("https://radio.example.org/stream")),
    Pause(),
    Resume(),
    Stop(),
    SetVolume(level=0),
    ShowCard(card=CARD),
    ClearDisplay(),
    SetLed(pattern=LedPattern.SOLID, color="#00ff00"),
)

EDGE_FRAMES = [
    ready(),
    ready(
        capabilities=SPEAKER | {Capability.DISPLAY},
        unregistered=frozenset({Capability.LED}),
        display=DisplayInfo(width=320, height=240),
        heartbeat_s=5,
    ),
    PartFrame(utterance_id="u1", seq=0, part=Speech(text="Hold the button for ten seconds.")),
    PartFrame(
        utterance_id="u1",
        seq=6,
        part=Speech(text="Hold the button for ten seconds.", audio=audio_payload()),
    ),
    PartFrame(utterance_id="u1", seq=1, part=Text(text="Hold the button for ten seconds. [1]")),
    PartFrame(utterance_id="u1", seq=2, part=CardPart(card=CARD)),
    PartFrame(
        utterance_id="u1",
        seq=3,
        part=MediaPart(url=AnyHttpUrl("https://radio.example.org/stream"), title="Radio"),
    ),
    PartFrame(utterance_id="u1", seq=4, part=CommandPart(command=SetVolume(level=30))),
    PartFrame(utterance_id="u1", seq=5, part=StatusPart(state=StatusState.SEARCHING)),
    TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED),
    TurnEnded(
        utterance_id="u1",
        status=TurnStatus.FAILED,
        code="chat_not_configured",
        message="no chat model is set up",
    ),
    TurnEnded(utterance_id="u1", status=TurnStatus.CANCELLED),
    Pong(),
    ErrorFrame(code="auth_failed", message="the device is not known", fatal=True),
    ErrorFrame(code="capability_missing", message="no microphone", ref="u1"),
    *(CommandFrame(id=f"c{n}", command=command) for n, command in enumerate(COMMANDS)),
]


def name_of(frame: Any) -> str:
    return f"{type(frame).__name__}-{getattr(frame, 'type', '')}"


class TestEveryFrameSurvivesTheWire:
    @pytest.mark.parametrize("frame", DEVICE_FRAMES, ids=name_of)
    def test_what_a_device_sends_is_what_the_edge_reads(self, frame: Any) -> None:
        assert decode_device_frame(encode_frame(frame)) == frame

    @pytest.mark.parametrize("frame", EDGE_FRAMES, ids=name_of)
    def test_what_the_edge_sends_is_what_a_device_reads(self, frame: Any) -> None:
        assert decode_edge_frame(encode_frame(frame)) == frame

    def test_bytes_are_read_as_well_as_text(self) -> None:
        assert decode_device_frame(encode_frame(Ping()).encode()) == Ping()

    def test_what_is_not_there_is_not_sent(self) -> None:
        assert json.loads(encode_frame(Utterance(id="u1", text="hi"))) == {
            "type": "utterance",
            "id": "u1",
            "text": "hi",
        }
        assert "null" not in encode_frame(TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED))

    @given(
        text=st.text(min_size=1, max_size=200).filter(lambda t: t.strip() != ""),
        identity=st.from_regex(r"[A-Za-z0-9_-]{1,64}", fullmatch=True),
    )
    def test_whatever_is_said_it_arrives_as_it_was_said(self, text: str, identity: str) -> None:
        frame = Utterance(id=identity, text=text)

        assert decode_device_frame(encode_frame(frame)) == frame


class TestWhatItTakesToKnowADevice:
    def test_the_capabilities_are_sent_in_the_same_order_however_they_were_written(self) -> None:
        one = hello(
            capabilities=frozenset({Capability.LED, Capability.AUDIO_IN, Capability.DISPLAY})
        )
        other = hello(
            capabilities=frozenset({Capability.DISPLAY, Capability.LED, Capability.AUDIO_IN})
        )

        assert encode_frame(one) == encode_frame(other)
        assert json.loads(encode_frame(one))["capabilities"] == ["audio_in", "display", "led"]

    def test_the_credential_is_never_in_what_is_printed(self) -> None:
        frame = hello(token="a-very-secret-credential")

        assert "a-very-secret-credential" not in repr(frame)
        assert "a-very-secret-credential" not in str(frame)

    def test_a_screen_that_is_described_must_be_declared(self) -> None:
        with pytest.raises(ValidationError, match="requires the 'display' capability"):
            hello(display=DisplayInfo(width=800, height=480))

    @pytest.mark.parametrize("hardware_id", ["", "has space", "x" * 129, "with/slash", "naïve"])
    def test_a_hardware_id_is_a_label_and_nothing_else(self, hardware_id: str) -> None:
        with pytest.raises(ValidationError):
            hello(hardware_id=hardware_id)

    def test_a_credential_is_there_and_is_not_a_book(self) -> None:
        for token in ("", "t" * 513):
            with pytest.raises(ValidationError):
                hello(token=token)

    def test_a_device_that_has_the_edges_view_of_it_can_be_told_what_was_left_out(self) -> None:
        told = ready(unregistered=frozenset({Capability.LED, Capability.DISPLAY}))

        assert json.loads(encode_frame(told))["unregistered"] == ["display", "led"]
        assert ready().unregistered == frozenset()

    def test_a_device_that_says_nothing_is_asked_to_say_it_is_there_every_so_often(self) -> None:
        assert ready().heartbeat_s == HEARTBEAT_SECONDS


class TestWhatIsSaidToTheDevice:
    @pytest.mark.parametrize("identity", ["", " ", "has space", "x" * 65, "a/b", "é"])
    def test_what_answers_an_utterance_is_named_by_a_plain_label(self, identity: str) -> None:
        with pytest.raises(ValidationError):
            Utterance(id=identity, text="hi")
        with pytest.raises(ValidationError):
            PartFrame(utterance_id=identity, seq=0, part=Speech(text="hi"))

    def test_what_a_person_said_is_stripped_and_is_not_blank_or_a_book(self) -> None:
        assert Utterance(id="u", text="  hello  ").text == "hello"
        for text in ("", "   ", "w" * (MAX_MESSAGE_CHARS + 1)):
            with pytest.raises(ValidationError):
                Utterance(id="u", text=text)


class TestARecording:
    def test_the_bytes_of_a_recording_survive_the_wire_exactly(self) -> None:
        # Every byte value, including ones that are not valid UTF-8 on their own: this is what
        # tells base64 (ser_json_bytes) apart from the default, which would mangle them.
        payload = audio_payload(data=bytes(range(256)))
        frame = AudioUtterance(id="u1", audio=payload)

        restored = decode_device_frame(encode_frame(frame))

        assert restored == frame
        assert isinstance(restored, AudioUtterance)
        assert restored.audio.data == bytes(range(256))

    def test_an_empty_recording_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            audio_payload(data=b"")

    @pytest.mark.parametrize("rate", [7999, 48001])
    def test_a_sample_rate_outside_what_speech_needs_is_rejected(self, rate: int) -> None:
        with pytest.raises(ValidationError):
            audio_payload(sample_rate=rate)

    def test_the_format_defaults_to_wav(self) -> None:
        assert audio_payload().format is AudioFormat.WAV

    def test_the_parts_of_an_answer_are_counted_from_nothing(self) -> None:
        with pytest.raises(ValidationError):
            PartFrame(utterance_id="u", seq=-1, part=Speech(text="hi"))

    def test_a_part_that_is_not_one_of_the_kinds_is_refused(self) -> None:
        wire = '{"type": "part", "utterance_id": "u", "seq": 0, "part": {"type": "hologram"}}'

        with pytest.raises(InvalidFrameError):
            decode_edge_frame(wire)

    def test_a_command_that_is_not_in_the_vocabulary_is_refused(self) -> None:
        wire = '{"type": "command", "id": "c", "command": {"name": "self_destruct"}}'

        with pytest.raises(InvalidFrameError):
            decode_edge_frame(wire)

    def test_a_command_with_a_value_out_of_range_is_refused(self) -> None:
        wire = '{"type": "command", "id": "c", "command": {"name": "set_volume", "level": 900}}'

        with pytest.raises(InvalidFrameError):
            decode_edge_frame(wire)


class TestWhatIsNotAFrame:
    @pytest.mark.parametrize(
        "wire",
        [
            "",
            "not json",
            "[]",
            "42",
            "null",
            "{}",
            '{"type": "dance"}',
            '{"type": "ping", "extra": 1}',
            '{"type": "utterance", "id": "u"}',
            '{"type": "hello", "protocol_version": "one"}',
        ],
    )
    def test_it_is_refused_whatever_it_is(self, wire: str) -> None:
        with pytest.raises(InvalidFrameError):
            decode_device_frame(wire)
        with pytest.raises(InvalidFrameError):
            decode_edge_frame(wire)

    def test_what_only_the_other_side_says_is_refused_from_this_one(self) -> None:
        with pytest.raises(InvalidFrameError):
            decode_device_frame(encode_frame(ready()))
        with pytest.raises(InvalidFrameError):
            decode_edge_frame(encode_frame(hello()))

    def test_the_refusal_says_where_and_why_and_never_what(self) -> None:
        wire = json.dumps(
            {
                "type": "hello",
                "protocol_version": "one",
                "hardware_id": "kitchen-01",
                "token": "a-very-secret-credential",
                "capabilities": [],
            }
        )

        with pytest.raises(InvalidFrameError) as refused:
            decode_device_frame(wire)

        assert "a-very-secret-credential" not in str(refused.value)
        assert "a-very-secret-credential" not in json.dumps(refused.value.details)
        assert refused.value.details["errors"][0]["where"] == "hello.protocol_version"

    def test_a_frame_is_at_most_what_the_protocol_allows_counted_in_bytes(self) -> None:
        padding = " " * (MAX_FRAME_BYTES - len('{"type": "ping"}'))
        at_the_limit = '{"type": "ping"}' + padding

        assert decode_device_frame(at_the_limit) == Ping()
        with pytest.raises(InvalidFrameError, match="at most"):
            decode_device_frame(at_the_limit + " ")
        # Two bytes for each character: within the limit as characters, not as bytes.
        with pytest.raises(InvalidFrameError, match="at most"):
            decode_device_frame('{"type": "ping"}' + "é" * (MAX_FRAME_BYTES // 2))


def test_the_application_close_codes_are_in_the_range_websockets_reserve_for_applications() -> None:
    assert {code.name for code in CloseCode if code >= 4000} == {
        "REPLACED",
        "IDLE",
        "UNSUPPORTED_VERSION",
    }
    assert all(4000 <= code <= 4999 for code in CloseCode if code >= 4000)
    assert int(CloseCode.NORMAL) == 1000


def test_this_is_the_first_version_of_the_protocol() -> None:
    assert PROTOCOL_VERSION == 1
