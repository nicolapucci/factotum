import re
from typing import Any, get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from factotum.contracts.audio import wav_of
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card, CardItem
from factotum.contracts.commands import (
    COMMAND_MODELS,
    COMMAND_VOCABULARY_VERSION,
    MAX_RESULT_DATA_CHARS,
    PLATFORM_COMMAND_MODELS,
    SKILL_COMMAND_MODELS,
    AdjustVolume,
    BaseCommand,
    ClearDisplay,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    GetState,
    LedPattern,
    MediaKind,
    Pause,
    PlayAudio,
    PlayUrl,
    Resume,
    SetLed,
    SetMute,
    SetVolume,
    ShowCard,
    SkillCommand,
    Speak,
    Stop,
    VolumeDirection,
)
from factotum.contracts.runner import SkillOutput

command_adapter: TypeAdapter[Any] = TypeAdapter(DeviceCommand)
skill_command_adapter: TypeAdapter[Any] = TypeAdapter(SkillCommand)

CARD = Card(title="Pasta al pomodoro", items=(CardItem(label="Tempo", value="20 min"),))
CLIP = wav_of(b"\x01\x00\x02\x00" * 50, 22_050)

EXAMPLES: list[tuple[BaseCommand, Capability]] = [
    (Speak(text="Ciao"), Capability.AUDIO_OUT),
    (PlayAudio(text="La cena è pronta.", audio=CLIP), Capability.AUDIO_OUT),
    (PlayUrl(url=AnyHttpUrl("https://radio.example.org/stream")), Capability.MEDIA_PLAYER),
    (Pause(), Capability.MEDIA_PLAYER),
    (Resume(), Capability.MEDIA_PLAYER),
    (Stop(), Capability.MEDIA_PLAYER),
    (SetVolume(level=30), Capability.AUDIO_OUT),
    (AdjustVolume(direction=VolumeDirection.DOWN, amount=10), Capability.AUDIO_OUT),
    (SetMute(muted=True), Capability.AUDIO_OUT),
    (GetState(), Capability.AUDIO_OUT),
    (ShowCard(card=CARD), Capability.DISPLAY),
    (ClearDisplay(), Capability.DISPLAY),
    (SetLed(pattern=LedPattern.PULSE, color="#00ff00"), Capability.LED),
]


@pytest.mark.parametrize(("command", "capability"), EXAMPLES, ids=lambda x: type(x).__name__)
def test_every_command_requires_the_capability_that_makes_it_possible(
    command: BaseCommand, capability: Capability
) -> None:
    assert command.requires is capability


def test_the_examples_cover_the_whole_vocabulary() -> None:
    assert {type(command) for command, _ in EXAMPLES} == set(COMMAND_MODELS)


def test_the_discriminated_union_lists_exactly_the_command_models() -> None:
    union = get_args(DeviceCommand)[0]

    assert set(get_args(union)) == set(COMMAND_MODELS)


def test_command_names_are_unique_snake_case_identifiers() -> None:
    names = [model.model_fields["name"].default for model in COMMAND_MODELS]

    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z]+(_[a-z]+)*", name) for name in names)


def test_the_vocabulary_is_versioned() -> None:
    # play_audio is what version 2 added, adjust_volume what version 3 did, and set_mute,
    # get_state and the data of a result what version 4 did
    assert COMMAND_VOCABULARY_VERSION == 4


@pytest.mark.parametrize(("command", "_"), EXAMPLES, ids=lambda x: type(x).__name__)
def test_a_command_round_trips_through_the_discriminated_union(
    command: BaseCommand, _: Capability
) -> None:
    restored = command_adapter.validate_json(command.model_dump_json())

    assert restored == command
    assert type(restored) is type(command)


def test_the_name_selects_the_command() -> None:
    command = command_adapter.validate_python({"name": "set_volume", "level": 30})

    assert isinstance(command, SetVolume)


def test_an_unknown_command_is_rejected() -> None:
    with pytest.raises(ValidationError):
        command_adapter.validate_python({"name": "self_destruct"})


class TestWhatASkillMayGive:
    def test_it_is_the_vocabulary_but_for_what_only_the_platform_makes(self) -> None:
        union = get_args(SkillCommand)[0]

        assert set(get_args(union)) == set(SKILL_COMMAND_MODELS)
        assert set(COMMAND_MODELS) - set(SKILL_COMMAND_MODELS) == {PlayAudio, GetState}
        assert set(PLATFORM_COMMAND_MODELS) == {PlayAudio, GetState}

    def test_a_clip_to_play_is_not_one_of_them(self) -> None:
        clip = PlayAudio(text="Hi.", audio=CLIP).model_dump(mode="json")

        with pytest.raises(ValidationError):
            skill_command_adapter.validate_python(clip)
        assert isinstance(command_adapter.validate_python(clip), PlayAudio)

    def test_what_a_script_prints_cannot_carry_one(self) -> None:
        printed = {
            "message": "Done.",
            "commands": [PlayAudio(text="Hi.", audio=CLIP).model_dump(mode="json")],
        }

        with pytest.raises(ValidationError):
            SkillOutput.model_validate(printed)

    def test_what_a_script_prints_may_carry_the_others(self) -> None:
        printed = {"message": "Done.", "commands": [{"name": "speak", "text": "Hi."}]}

        assert SkillOutput.model_validate(printed).commands == (Speak(text="Hi."),)

    def test_a_question_to_a_device_is_not_one_of_them(self) -> None:
        """The answer is for the skill of the platform that asked: a script, or a plan, is never
        given what a device answered, so there is nothing for it to ask for."""
        question = {"name": "get_state"}

        with pytest.raises(ValidationError):
            skill_command_adapter.validate_python(question)
        with pytest.raises(ValidationError):
            SkillOutput.model_validate({"message": "Done.", "commands": [question]})
        assert isinstance(command_adapter.validate_python(question), GetState)

    def test_the_sound_may_be_muted_by_a_skill(self) -> None:
        said = {"name": "set_mute", "muted": True}

        assert skill_command_adapter.validate_python(said) == SetMute(muted=True)


class TestAClipToPlay:
    def test_it_says_what_it_says_in_words_as_well_as_in_sound(self) -> None:
        with pytest.raises(ValidationError):
            PlayAudio.model_validate({"name": "play_audio", "audio": CLIP.model_dump(mode="json")})
        with pytest.raises(ValidationError):
            PlayAudio(text="", audio=CLIP)

    def test_it_is_a_clip(self) -> None:
        with pytest.raises(ValidationError):
            PlayAudio.model_validate({"name": "play_audio", "text": "Hi."})

    def test_what_it_says_is_as_long_as_what_speak_says(self) -> None:
        assert len(PlayAudio(text="a" * 2000, audio=CLIP).text) == 2000
        with pytest.raises(ValidationError):
            PlayAudio(text="a" * 2001, audio=CLIP)

    def test_the_sound_travels_as_base64_in_json(self) -> None:
        said = PlayAudio(text="Hi.", audio=CLIP).model_dump(mode="json")

        assert isinstance(said["audio"]["data"], str)
        assert command_adapter.validate_python(said).audio.data == CLIP.data


class TestValidation:
    @pytest.mark.parametrize("level", [-1, 101])
    def test_the_volume_stays_between_0_and_100(self, level: int) -> None:
        with pytest.raises(ValidationError):
            SetVolume(level=level)

    @pytest.mark.parametrize("level", [0, 100])
    def test_the_volume_bounds_are_inclusive(self, level: int) -> None:
        assert SetVolume(level=level).level == level

    @pytest.mark.parametrize("amount", [0, -5, 101, 1000])
    def test_an_amount_of_the_volume_is_from_1_to_100_points(self, amount: int) -> None:
        with pytest.raises(ValidationError):
            AdjustVolume(direction=VolumeDirection.UP, amount=amount)

    @pytest.mark.parametrize("amount", [1, 10, 100])
    def test_the_bounds_of_an_amount_of_the_volume_are_inclusive(self, amount: int) -> None:
        assert AdjustVolume(direction=VolumeDirection.DOWN, amount=amount).amount == amount

    @given(st.integers())
    def test_an_amount_of_the_volume_is_a_whole_number_of_points_from_1_to_100(
        self, amount: int
    ) -> None:
        if 1 <= amount <= 100:
            assert AdjustVolume(direction=VolumeDirection.UP, amount=amount).amount == amount
        else:
            with pytest.raises(ValidationError):
                AdjustVolume(direction=VolumeDirection.UP, amount=amount)

    @pytest.mark.parametrize("amount", [0.5, "much"])
    def test_an_amount_of_the_volume_is_a_whole_number(self, amount: object) -> None:
        with pytest.raises(ValidationError):
            AdjustVolume.model_validate(
                {"name": "adjust_volume", "direction": "up", "amount": amount}
            )

    def test_an_amount_that_nobody_said_is_not_there_and_the_device_has_its_own_step(self) -> None:
        assert AdjustVolume(direction=VolumeDirection.UP).amount is None
        said = command_adapter.validate_python({"name": "adjust_volume", "direction": "down"})
        assert said == AdjustVolume(direction=VolumeDirection.DOWN)
        assert "amount" in said.model_dump()  # an unsaid amount travels as null, not as a number

    @pytest.mark.parametrize("direction", ["", "louder", "UP", None])
    def test_the_direction_is_up_or_down_and_nothing_else(self, direction: object) -> None:
        with pytest.raises(ValidationError):
            AdjustVolume.model_validate({"name": "adjust_volume", "direction": direction})

    def test_a_change_of_the_volume_says_which_way(self) -> None:
        with pytest.raises(ValidationError):
            AdjustVolume.model_validate({"name": "adjust_volume"})
        with pytest.raises(ValidationError):
            AdjustVolume.model_validate({"name": "adjust_volume", "amount": 10})

    def test_the_name_selects_it_and_a_skill_may_give_it(self) -> None:
        said = {"name": "adjust_volume", "direction": "down", "amount": 20}
        wanted = AdjustVolume(direction=VolumeDirection.DOWN, amount=20)

        assert command_adapter.validate_python(said) == wanted
        assert skill_command_adapter.validate_python(said) == wanted
        assert SkillOutput.model_validate({"message": "Done.", "commands": [said]}).commands == (
            wanted,
        )

    def test_muting_says_whether_the_sound_is_to_be_muted_or_brought_back(self) -> None:
        assert SetMute(muted=True).muted is True
        assert SetMute(muted=False).muted is False
        with pytest.raises(ValidationError):
            SetMute.model_validate({"name": "set_mute"})
        with pytest.raises(ValidationError):
            SetMute.model_validate({"name": "set_mute", "muted": "maybe"})

    def test_it_has_no_number_of_points_that_a_skill_could_have_to_know(self) -> None:
        """The skill is the interface to the device and has no step to give (ADR 0028): there is
        no field for one other than the amount a person said."""
        assert set(AdjustVolume.model_fields) == {"name", "direction", "amount"}

    def test_speech_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            Speak(text="")

    @pytest.mark.parametrize("language", ["it", "en-GB", "yue"])
    def test_language_accepts_bcp47_style_tags(self, language: str) -> None:
        assert Speak(text="ciao", language=language).language == language

    @pytest.mark.parametrize("language", ["italiano", "EN", "en_GB"])
    def test_language_rejects_other_shapes(self, language: str) -> None:
        with pytest.raises(ValidationError):
            Speak(text="ciao", language=language)

    @pytest.mark.parametrize("color", ["red", "#fff", "00ff00", "#00ff0g"])
    def test_led_color_must_be_a_hex_rgb_value(self, color: str) -> None:
        with pytest.raises(ValidationError):
            SetLed(pattern=LedPattern.SOLID, color=color)

    def test_a_media_url_must_be_http_or_https(self) -> None:
        with pytest.raises(ValidationError):
            PlayUrl.model_validate({"url": "file:///etc/passwd"})

    def test_media_defaults_to_audio(self) -> None:
        command = PlayUrl(url=AnyHttpUrl("https://radio.example.org/stream"))

        assert command.kind is MediaKind.AUDIO

    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            Pause.model_validate({"name": "pause", "force": True})


class TestCard:
    def test_a_card_needs_a_title(self) -> None:
        with pytest.raises(ValidationError):
            Card(title="")

    def test_a_card_holds_at_most_twenty_items(self) -> None:
        items = tuple(CardItem(label=f"item {i}") for i in range(21))

        with pytest.raises(ValidationError):
            Card(title="Too long", items=items)

    def test_the_image_must_be_an_http_url(self) -> None:
        with pytest.raises(ValidationError):
            Card.model_validate({"title": "Recipe", "image_url": "ftp://example.org/x.png"})


class TestCommandResult:
    def test_ok_is_a_success(self) -> None:
        assert CommandResult.ok().succeeded

    def test_a_result_has_no_data_unless_a_device_answers_something(self) -> None:
        assert CommandResult.ok().data is None
        assert CommandResult(status=CommandStatus.FAILED, detail="why").data is None

    def test_a_device_may_answer_with_a_little_data(self) -> None:
        answered = CommandResult.ok({"volume": 40, "muted": False, "timers": [{"left_s": 90}]})

        assert answered.succeeded
        assert answered.data == {"volume": 40, "muted": False, "timers": [{"left_s": 90}]}
        assert CommandResult.model_validate_json(answered.model_dump_json()) == answered

    def test_what_is_answered_is_not_more_than_a_device_would_say(self) -> None:
        with pytest.raises(ValidationError, match="at most"):
            CommandResult.ok({"x": "y" * MAX_RESULT_DATA_CHARS})
        assert CommandResult.ok({"x": "y" * (MAX_RESULT_DATA_CHARS - 20)}).data is not None

    def test_what_is_answered_is_data_and_not_a_thing(self) -> None:
        with pytest.raises(ValidationError):
            CommandResult.model_validate({"status": "ok", "data": {"x": {1, 2}}})
        with pytest.raises(ValidationError):
            CommandResult.model_validate({"status": "ok", "data": ["not", "a", "mapping"]})

    @pytest.mark.parametrize("status", [s for s in CommandStatus if s is not CommandStatus.OK])
    def test_every_other_status_is_a_failure(self, status: CommandStatus) -> None:
        assert not CommandResult(status=status, detail="why").succeeded
