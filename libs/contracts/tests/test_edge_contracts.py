import json
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import AnyHttpUrl, ValidationError

from factotum.contracts.audio import (
    BYTES_PER_SAMPLE,
    MAX_AUDIO_BYTES,
    MAX_CHUNK_BYTES,
    WAV_HEADER_BYTES,
    AudioFormat,
    AudioPayload,
    StreamFormat,
    wav_of,
)
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.cards import Card, CardItem
from factotum.contracts.commands import (
    AdjustVolume,
    AlarmDay,
    CancelAlarm,
    CancelTimer,
    ClearDisplay,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    GetState,
    LedPattern,
    MoveAlarm,
    Pause,
    PlayAudio,
    PlayUrl,
    PostponeAlarm,
    Resume,
    SetAlarm,
    SetLed,
    SetMute,
    SetTimer,
    SetVolume,
    ShowCard,
    Speak,
    Stop,
    StopRinging,
    VolumeDirection,
    Weekday,
)
from factotum.contracts.conversation import MAX_MESSAGE_CHARS
from factotum.contracts.edge import (
    FEATURE_NEEDS,
    HEARTBEAT_SECONDS,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    Ack,
    AudioChunk,
    AudioEnd,
    AudioStart,
    AudioUtterance,
    Bye,
    Cancel,
    ClientInfo,
    CloseCode,
    CommandFrame,
    CommandRequest,
    ErrorFrame,
    Feature,
    Heard,
    Hello,
    PartFrame,
    Ping,
    Pong,
    Ready,
    TurnEnded,
    TurnStatus,
    Utterance,
    VoiceChunk,
    VoiceEnd,
    VoiceStart,
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
    AudioStart(id="u3", sample_rate=16000),
    AudioStart(id="u3", format=StreamFormat.PCM_S16LE, sample_rate=44100),
    AudioChunk(id="u3", seq=0, data=b"\x00\x01\xfe\xff not really samples"),
    AudioChunk(id="u3", seq=41, data=bytes(range(256))),
    AudioEnd(id="u3"),
    hello(features=frozenset({Feature.VOICE_STREAM})),
    Cancel(id="u1"),
    Ack(id="c1", result=CommandResult.ok()),
    Ack(id="c3", result=CommandResult.ok({"volume": 40, "muted": False})),
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
    AdjustVolume(direction=VolumeDirection.DOWN, amount=15),
    AdjustVolume(direction=VolumeDirection.UP),
    SetMute(muted=True),
    GetState(),
    SetTimer(seconds=90, text="Time is up.", audio=wav_of(b"\x01\x00" * 40, 22_050)),
    CancelTimer(),
    CancelTimer(seconds=90),
    SetAlarm(
        hour=6,
        minute=45,
        day=AlarmDay(weekday=Weekday.TUESDAY),
        text="Wake up.",
        audio=wav_of(b"\x01\x00" * 40, 22_050),
    ),
    CancelAlarm(hour=6, minute=45),
    PostponeAlarm(),
    PostponeAlarm(seconds=300, hour=6, minute=45),
    MoveAlarm(hour=8, minute=0),
    MoveAlarm(hour=8, minute=0, day=AlarmDay(in_days=1), from_hour=6, from_minute=45),
    StopRinging(),
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
    ready(features=frozenset({Feature.VOICE_STREAM})),
    Heard(utterance_id="u3", text="how do I reset the router?"),
    VoiceStart(utterance_id="u1", sample_rate=22050),
    VoiceStart(utterance_id="u1", format=StreamFormat.PCM_S16LE, sample_rate=16000),
    VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=b"\x00\x01\xfe\xff"),
    VoiceChunk(utterance_id="u1", seq=41, part_seq=6, data=bytes(range(256))),
    VoiceEnd(utterance_id="u1"),
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


class TestAStreamOfAudio:
    def test_the_samples_of_a_chunk_survive_the_wire_exactly(self) -> None:
        # Every byte value, as for a recording: base64 is what keeps the ones that are not valid
        # UTF-8 on their own.
        chunk = AudioChunk(id="u1", seq=3, data=bytes(range(256)))

        restored = decode_device_frame(encode_frame(chunk))

        assert restored == chunk
        assert isinstance(restored, AudioChunk)
        assert restored.data == bytes(range(256))

    def test_what_goes_over_the_wire_is_plain_json_with_the_samples_in_base64(self) -> None:
        start = AudioStart(id="u1", sample_rate=16000)
        chunk = AudioChunk(id="u1", seq=0, data=b"\x00\x01\x02")

        assert json.loads(encode_frame(start)) == {
            "type": "audio_start",
            "id": "u1",
            "format": "pcm_s16le",
            "sample_rate": 16000,
        }
        assert json.loads(encode_frame(chunk)) == {
            "type": "audio_chunk",
            "id": "u1",
            "seq": 0,
            "data": "AAEC",
        }
        assert json.loads(encode_frame(AudioEnd(id="u1"))) == {"type": "audio_end", "id": "u1"}

    def test_the_samples_are_16_bit_little_endian_unless_said_otherwise_and_nothing_else_is_said(
        self,
    ) -> None:
        assert AudioStart(id="u1", sample_rate=16000).format is StreamFormat.PCM_S16LE
        wire = '{"type": "audio_start", "id": "u1", "format": "mp3", "sample_rate": 16000}'

        with pytest.raises(InvalidFrameError):
            decode_device_frame(wire)

    @pytest.mark.parametrize("rate", [0, -16000, 7999, 48001])
    def test_a_sample_rate_outside_what_speech_needs_is_refused(self, rate: int) -> None:
        with pytest.raises(ValidationError):
            AudioStart(id="u1", sample_rate=rate)

    @pytest.mark.parametrize("rate", [8000, 16000, 22050, 44100, 48000])
    def test_every_rate_of_a_clip_is_a_rate_of_a_stream(self, rate: int) -> None:
        assert (
            AudioStart(id="u1", sample_rate=rate).sample_rate
            == audio_payload(sample_rate=rate).sample_rate
        )

    def test_a_chunk_has_samples_and_a_place_in_the_stream(self) -> None:
        with pytest.raises(ValidationError):
            AudioChunk(id="u1", seq=0, data=b"")
        with pytest.raises(ValidationError):
            AudioChunk(id="u1", seq=-1, data=b"\x00\x00")

    def test_a_chunk_over_its_size_is_still_a_frame_so_that_the_stream_can_be_ended_for_it(
        self,
    ) -> None:
        # The edge ends the stream of a chunk that is too big and keeps the connection; a frame the
        # contract refused would be a protocol error that closes it.
        chunk = AudioChunk(id="u1", seq=0, data=bytes(2 * MAX_CHUNK_BYTES))

        assert decode_device_frame(encode_frame(chunk)) == chunk
        assert len(encode_frame(chunk)) < MAX_FRAME_BYTES

    @pytest.mark.parametrize("identity", ["", " ", "has space", "x" * 65, "a/b", "é"])
    def test_a_stream_is_named_by_a_plain_label_like_any_utterance(self, identity: str) -> None:
        with pytest.raises(ValidationError):
            AudioStart(id=identity, sample_rate=16000)
        with pytest.raises(ValidationError):
            AudioChunk(id=identity, seq=0, data=b"\x00\x00")
        with pytest.raises(ValidationError):
            AudioEnd(id=identity)

    def test_what_a_device_says_to_stream_is_not_something_the_edge_says(self) -> None:
        for frame in (
            AudioStart(id="u1", sample_rate=16000),
            AudioChunk(id="u1", seq=0, data=b"\x00\x00"),
            AudioEnd(id="u1"),
        ):
            with pytest.raises(InvalidFrameError):
                decode_edge_frame(encode_frame(frame))

    @given(
        data=st.binary(min_size=1, max_size=2000),
        seq=st.integers(min_value=0, max_value=100_000),
    )
    def test_whatever_the_samples_they_arrive_as_they_were_sent(
        self, data: bytes, seq: int
    ) -> None:
        frame = AudioChunk(id="u1", seq=seq, data=data)

        assert decode_device_frame(encode_frame(frame)) == frame


class TestWhatWasHeard:
    def test_it_says_which_utterance_and_what_it_was_taken_for(self) -> None:
        heard = Heard(utterance_id="u1", text="  how do I reset the router?  ")

        assert json.loads(encode_frame(heard)) == {
            "type": "heard",
            "utterance_id": "u1",
            "text": "how do I reset the router?",
        }

    def test_it_is_not_blank_and_is_not_a_book(self) -> None:
        for text in ("", "   ", "w" * (MAX_MESSAGE_CHARS + 1)):
            with pytest.raises(ValidationError):
                Heard(utterance_id="u1", text=text)

    @pytest.mark.parametrize("identity", ["", " ", "has space", "x" * 65, "a/b", "é"])
    def test_it_names_the_utterance_by_a_plain_label(self, identity: str) -> None:
        with pytest.raises(ValidationError):
            Heard(utterance_id=identity, text="hello")

    def test_what_only_the_edge_says_is_not_something_a_device_says(self) -> None:
        with pytest.raises(InvalidFrameError):
            decode_device_frame(encode_frame(Heard(utterance_id="u1", text="hello")))


class TestWhatAFeatureIs:
    def test_a_device_that_says_nothing_of_features_takes_none(self) -> None:
        assert hello().features == frozenset()
        assert ready().features == frozenset()

    def test_what_is_nothing_is_not_said_so_that_a_peer_from_before_features_still_reads_it(
        self,
    ) -> None:
        assert "features" not in json.loads(encode_frame(hello()))
        assert "features" not in json.loads(encode_frame(ready()))

    def test_what_is_taken_is_said_by_name(self) -> None:
        said = json.loads(encode_frame(hello(features=frozenset({Feature.VOICE_STREAM}))))
        told = json.loads(encode_frame(ready(features=frozenset({Feature.VOICE_STREAM}))))

        assert said["features"] == ["voice_stream"]
        assert told["features"] == ["voice_stream"]

    def test_a_peer_that_sent_no_features_is_read_as_one_that_takes_none(self) -> None:
        wire = json.dumps(
            {
                "type": "hello",
                "protocol_version": 1,
                "hardware_id": "kitchen-01",
                "token": "a-credential",
                "capabilities": ["audio_in", "audio_out"],
            }
        )

        frame = decode_device_frame(wire)

        assert isinstance(frame, Hello)
        assert frame.features == frozenset()

    def test_a_feature_that_is_not_known_is_refused(self) -> None:
        wire = json.dumps(
            {
                "type": "hello",
                "protocol_version": 1,
                "hardware_id": "kitchen-01",
                "token": "a-credential",
                "capabilities": ["audio_out"],
                "features": ["hologram"],
            }
        )

        with pytest.raises(InvalidFrameError):
            decode_device_frame(wire)

    def test_every_feature_says_what_a_device_needs_to_take_it(self) -> None:
        assert set(FEATURE_NEEDS) == set(Feature)

    def test_a_voice_is_for_a_device_that_has_a_speaker(self) -> None:
        assert FEATURE_NEEDS[Feature.VOICE_STREAM] is Capability.AUDIO_OUT
        with pytest.raises(ValidationError, match="requires the 'audio_out' capability"):
            hello(
                capabilities=frozenset({Capability.AUDIO_IN}),
                features=frozenset({Feature.VOICE_STREAM}),
            )

    def test_what_is_declared_is_the_same_whichever_way_it_was_written(self) -> None:
        one = hello(features=frozenset({Feature.VOICE_STREAM}))
        other = hello(features=frozenset([Feature.VOICE_STREAM, Feature.VOICE_STREAM]))

        assert encode_frame(one) == encode_frame(other)

    def test_a_clip_to_play_is_for_a_device_that_has_a_speaker_too(self) -> None:
        assert FEATURE_NEEDS[Feature.PLAY_AUDIO] is Capability.AUDIO_OUT
        with pytest.raises(ValidationError, match="requires the 'audio_out' capability"):
            hello(
                capabilities=frozenset({Capability.AUDIO_IN}),
                features=frozenset({Feature.PLAY_AUDIO}),
            )

    def test_both_features_are_said_by_name_in_order(self) -> None:
        both = frozenset({Feature.VOICE_STREAM, Feature.PLAY_AUDIO})

        assert json.loads(encode_frame(hello(features=both)))["features"] == [
            "play_audio",
            "voice_stream",
        ]


class TestACommandThatCarriesAClip:
    def test_it_is_one_frame_whatever_the_clip(self) -> None:
        # The longest clip there is, in base64, is a frame of the protocol and not more than one.
        clip = wav_of(b"\x01\x02" * ((MAX_AUDIO_BYTES - WAV_HEADER_BYTES) // 2), 22_050)
        frame = CommandFrame(id="c1", command=PlayAudio(text="Hi.", audio=clip))

        wire = encode_frame(frame)
        restored = decode_edge_frame(wire)

        assert len(wire) < MAX_FRAME_BYTES
        assert isinstance(restored, CommandFrame)
        assert isinstance(restored.command, PlayAudio)
        assert restored.command.audio.data == clip.data

    def test_a_request_to_the_edge_carries_it_as_well(self) -> None:
        clip = wav_of(b"\x01\x02" * 100, 22_050)
        request = CommandRequest(command=PlayAudio(text="Hi.", audio=clip))

        restored = CommandRequest.model_validate_json(request.model_dump_json())

        assert restored == request


class TestTheVoiceOfAnAnswer:
    def test_the_samples_of_a_chunk_survive_the_wire_exactly(self) -> None:
        # Every byte value, as for a recording: base64 is what keeps the ones that are not valid
        # UTF-8 on their own.
        chunk = VoiceChunk(utterance_id="u1", seq=3, part_seq=1, data=bytes(range(256)))

        restored = decode_edge_frame(encode_frame(chunk))

        assert restored == chunk
        assert isinstance(restored, VoiceChunk)
        assert restored.data == bytes(range(256))

    def test_what_goes_over_the_wire_is_plain_json_with_the_samples_in_base64(self) -> None:
        assert json.loads(encode_frame(VoiceStart(utterance_id="u1", sample_rate=22050))) == {
            "type": "voice_start",
            "utterance_id": "u1",
            "format": "pcm_s16le",
            "sample_rate": 22050,
        }
        assert json.loads(
            encode_frame(VoiceChunk(utterance_id="u1", seq=0, part_seq=2, data=b"\x00\x01\x02\x03"))
        ) == {
            "type": "voice_chunk",
            "utterance_id": "u1",
            "seq": 0,
            "part_seq": 2,
            "data": "AAECAw==",
        }
        assert json.loads(encode_frame(VoiceEnd(utterance_id="u1"))) == {
            "type": "voice_end",
            "utterance_id": "u1",
        }

    def test_the_samples_are_16_bit_little_endian_unless_said_otherwise_and_nothing_else_is_said(
        self,
    ) -> None:
        assert VoiceStart(utterance_id="u1", sample_rate=16000).format is StreamFormat.PCM_S16LE
        wire = (
            '{"type": "voice_start", "utterance_id": "u1", "format": "mp3", "sample_rate": 16000}'
        )

        with pytest.raises(InvalidFrameError):
            decode_edge_frame(wire)

    @pytest.mark.parametrize("rate", [0, -22050, 7999, 48001])
    def test_a_sample_rate_outside_what_speech_needs_is_refused(self, rate: int) -> None:
        with pytest.raises(ValidationError):
            VoiceStart(utterance_id="u1", sample_rate=rate)

    def test_a_chunk_has_a_place_in_the_track_and_one_in_the_answer(self) -> None:
        with pytest.raises(ValidationError):
            VoiceChunk(utterance_id="u1", seq=-1, part_seq=0, data=b"\x00\x00")
        with pytest.raises(ValidationError):
            VoiceChunk(utterance_id="u1", seq=0, part_seq=-1, data=b"\x00\x00")

    @pytest.mark.parametrize("size", [0, 1, 3, 4001])
    def test_a_chunk_is_whole_samples_and_is_not_nothing(self, size: int) -> None:
        with pytest.raises(ValidationError):
            VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=bytes(size))

    def test_a_chunk_is_a_sample_at_least_and_at_most_what_a_chunk_of_a_stream_holds(self) -> None:
        smallest = VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=bytes(BYTES_PER_SAMPLE))
        biggest = VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=bytes(MAX_CHUNK_BYTES))

        assert decode_edge_frame(encode_frame(smallest)) == smallest
        assert decode_edge_frame(encode_frame(biggest)) == biggest
        assert len(encode_frame(biggest)) < MAX_FRAME_BYTES
        with pytest.raises(ValidationError):
            VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=bytes(MAX_CHUNK_BYTES + 2))

    @pytest.mark.parametrize("identity", ["", " ", "has space", "x" * 65, "a/b", "é"])
    def test_a_voice_belongs_to_an_utterance_named_by_a_plain_label(self, identity: str) -> None:
        with pytest.raises(ValidationError):
            VoiceStart(utterance_id=identity, sample_rate=16000)
        with pytest.raises(ValidationError):
            VoiceChunk(utterance_id=identity, seq=0, part_seq=0, data=b"\x00\x00")
        with pytest.raises(ValidationError):
            VoiceEnd(utterance_id=identity)

    def test_what_the_edge_says_of_a_voice_is_not_something_a_device_says(self) -> None:
        for frame in (
            VoiceStart(utterance_id="u1", sample_rate=16000),
            VoiceChunk(utterance_id="u1", seq=0, part_seq=0, data=b"\x00\x00"),
            VoiceEnd(utterance_id="u1"),
        ):
            with pytest.raises(InvalidFrameError):
                decode_device_frame(encode_frame(frame))

    @given(
        data=st.binary(min_size=2, max_size=2000).map(lambda b: b[: len(b) - len(b) % 2]),
        seq=st.integers(min_value=0, max_value=100_000),
        part_seq=st.integers(min_value=0, max_value=1_000),
    )
    def test_whatever_the_samples_they_arrive_as_they_were_sent(
        self, data: bytes, seq: int, part_seq: int
    ) -> None:
        frame = VoiceChunk(utterance_id="u1", seq=seq, part_seq=part_seq, data=data)

        assert decode_edge_frame(encode_frame(frame)) == frame


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
            '{"type": "audio_start", "id": "u"}',
            '{"type": "audio_chunk", "id": "u", "seq": 0}',
            '{"type": "audio_chunk", "id": "u", "seq": 0, "data": "not base64 !"}',
            '{"type": "audio_chunk", "id": "u", "seq": "first", "data": "AAEC"}',
            '{"type": "audio_end"}',
            '{"type": "heard", "utterance_id": "u"}',
            '{"type": "voice_start", "utterance_id": "u"}',
            '{"type": "voice_chunk", "utterance_id": "u", "seq": 0, "part_seq": 0}',
            '{"type": "voice_chunk", "utterance_id": "u", "seq": 0, "data": "AAEC"}',
            '{"type": "voice_chunk", "utterance_id": "u", "seq": 0, "part_seq": 0, "data": "AAEC"}',
            '{"type": "voice_end"}',
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
