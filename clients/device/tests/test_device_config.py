"""What a device is told about itself: read from a file and from the environment, and refused in a
line that names the setting when it says what cannot be."""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.client_audio import ACCESS_KEY_VARIABLE, WAKE_WORDS, Engine
from factotum.contracts.capabilities import Capability
from factotum.device.cli import EXAMPLE
from factotum.device.config import (
    CONFIG_VARIABLE,
    DEFAULT_URL,
    TOKEN_VARIABLE,
    URL_VARIABLE,
    ConfigError,
    Mode,
    default_path,
    load_config,
    parse_config,
)

MINIMAL = "device:\n  hardware_id: kitchen-01\n  token: kitchen-credential\n"


class TestWhatTheDefaultsAre:
    def test_a_device_that_says_who_it_is_is_a_speaker_that_a_key_wakes(self) -> None:
        config = parse_config(MINIMAL, {})

        assert config.edge.url == DEFAULT_URL
        assert config.device.hardware_id == "kitchen-01"
        assert config.device.capabilities == {Capability.AUDIO_IN, Capability.AUDIO_OUT}
        assert (config.listening.mode, config.audio.volume) == (Mode.PUSH_TO_TALK, 100)
        assert config.audio.volume_step == 10
        assert (config.audio.input_device, config.audio.output_device) == (None, None)
        assert config.listening.listen_while_answering is False
        assert (config.reconnect.first_s, config.reconnect.max_s) == (1.0, 30.0)
        assert config.log_level == "INFO"

    def test_the_credential_is_given_back_and_is_not_written_where_a_config_is_shown(self) -> None:
        config = parse_config(MINIMAL, {})

        assert config.token == "kitchen-credential"
        assert "kitchen-credential" not in repr(config)
        assert "kitchen-credential" not in config.model_dump_json()

    def test_what_a_device_can_do_follows_what_it_declares(self) -> None:
        listening = parse_config(MINIMAL, {})
        spoken_through = parse_config(MINIMAL + "  capabilities: [audio_out]\n", {})

        assert (listening.can_hear, listening.can_speak) == (True, True)
        assert (spoken_through.can_hear, spoken_through.can_speak) == (False, True)

    def test_the_file_that_init_writes_is_one_that_is_read(self) -> None:
        config = parse_config(EXAMPLE, {TOKEN_VARIABLE: "a-credential"})

        assert config.device.hardware_id == "kitchen-01"
        assert config.token == "a-credential"
        assert config.listening.mode is Mode.PUSH_TO_TALK
        assert config.listening.engine is Engine.OPENWAKEWORD


class TestWhatTheEnvironmentSays:
    def test_the_credential_of_the_environment_wins_over_the_file(self) -> None:
        config = parse_config(MINIMAL, {TOKEN_VARIABLE: "from-the-environment"})

        assert config.token == "from-the-environment"

    def test_the_credential_may_be_only_in_the_environment(self) -> None:
        text = "device:\n  hardware_id: kitchen-01\n"

        assert parse_config(text, {TOKEN_VARIABLE: "x"}).token == "x"

    def test_a_device_with_no_credential_anywhere_says_where_to_put_it(self) -> None:
        with pytest.raises(ConfigError, match=TOKEN_VARIABLE):
            parse_config("device:\n  hardware_id: kitchen-01\n", {})

    def test_the_address_of_the_edge_may_be_changed_without_touching_the_file(self) -> None:
        config = parse_config(MINIMAL, {URL_VARIABLE: "wss://edge.example/v1/connect"})

        assert config.edge.url == "wss://edge.example/v1/connect"

    def test_the_key_of_porcupine_is_the_environments(self) -> None:
        text = MINIMAL + "listening:\n  mode: wake_word\n  engine: porcupine\n"

        config = parse_config(text, {ACCESS_KEY_VARIABLE: "a-key"})

        assert config.listening.mode is Mode.WAKE_WORD
        assert config.listening.access_key is not None
        assert config.listening.access_key.get_secret_value() == "a-key"

    def test_the_key_of_the_environment_wins_over_the_one_in_the_file(self) -> None:
        text = (
            MINIMAL
            + "listening:\n  mode: wake_word\n  engine: porcupine\n  access_key: from-the-file\n"
        )

        config = parse_config(text, {ACCESS_KEY_VARIABLE: "from-the-environment"})

        assert config.listening.access_key is not None
        assert config.listening.access_key.get_secret_value() == "from-the-environment"

    def test_the_key_is_not_in_what_is_shown_of_the_settings(self) -> None:
        text = MINIMAL + "listening:\n  mode: wake_word\n  engine: porcupine\n"

        config = parse_config(text, {ACCESS_KEY_VARIABLE: "a-key-that-is-secret"})

        assert "a-key-that-is-secret" not in repr(config)
        assert "a-key-that-is-secret" not in config.model_dump_json()

    def test_porcupine_with_no_key_is_refused_and_it_is_said_who_has_keys_and_what_needs_none(
        self,
    ) -> None:
        text = MINIMAL + "listening:\n  mode: wake_word\n  engine: porcupine\n"

        with pytest.raises(
            ConfigError,
            match=r"listening\.engine porcupine needs an access key.*companies.*needs none",
        ):
            parse_config(text, {})

    def test_a_device_with_no_microphone_needs_no_key_for_a_word_it_will_not_wait_for(self) -> None:
        text = (
            MINIMAL
            + "  capabilities: [audio_out]\nlistening:\n  mode: wake_word\n  engine: porcupine\n"
        )

        assert parse_config(text, {}).can_hear is False


class TestTheWordThatItWaitsFor:
    def settings(self, listening: str = "") -> str:
        return MINIMAL + "listening:\n  mode: wake_word\n" + listening

    def test_openwakeword_listens_unless_it_is_said_and_it_needs_no_key_and_no_folder(self) -> None:
        listening = parse_config(self.settings(), {}).listening

        assert (listening.engine, listening.word) == (Engine.OPENWAKEWORD, "hey_jarvis")
        assert (listening.threshold, listening.patience) == (0.5, 1)
        assert (listening.models_dir, listening.access_key) == (None, None)

    def test_the_word_that_porcupine_listens_for_unless_it_is_said_is_the_one_it_has_a_keyword_for(
        self,
    ) -> None:
        listening = parse_config(
            self.settings("  engine: porcupine\n  access_key: a-key\n"), {}
        ).listening

        assert listening.word == "jarvis"

    @pytest.mark.parametrize("word", sorted(WAKE_WORDS))
    def test_every_word_that_there_is_a_model_for_is_one_to_wait_for(self, word: str) -> None:
        listening = parse_config(self.settings(f"  wake_word: {word}\n"), {}).listening

        assert listening.word == word

    def test_a_word_that_there_is_no_model_for_is_refused_with_those_that_there_are(self) -> None:
        with pytest.raises(ConfigError) as error:
            parse_config(self.settings("  wake_word: computer\n"), {})

        said = str(error.value)
        assert "listening.wake_word 'computer'" in said
        assert "alexa, hey_jarvis, hey_mycroft, hey_rhasspy" in said
        assert "listening.engine porcupine" in said  # which has others

    def test_the_words_of_porcupine_are_its_own(self) -> None:
        text = self.settings("  engine: porcupine\n  wake_word: computer\n  access_key: k\n")

        assert parse_config(text, {}).listening.word == "computer"

    @pytest.mark.parametrize(
        "text",
        [
            MINIMAL + "listening:\n  wake_word: computer\n",
            MINIMAL
            + "  capabilities: [audio_out]\nlistening:\n  mode: wake_word\n  wake_word: x\n",
        ],
        ids=["push to talk", "no microphone"],
    )
    def test_a_word_that_nothing_waits_for_is_not_looked_at(self, text: str) -> None:
        assert parse_config(text, {}).listening.wake_word is not None

    @pytest.mark.parametrize(
        "setting",
        ["threshold: 0", "threshold: -0.2", "threshold: 1.5", "patience: 0", "patience: 11"],
    )
    def test_a_threshold_that_is_not_a_score_and_a_patience_that_is_not_a_count_are_named(
        self, setting: str
    ) -> None:
        with pytest.raises(ConfigError) as error:
            parse_config(self.settings(f"  {setting}\n"), {})

        assert f"listening.{setting.split(':')[0]}" in str(error.value)

    @pytest.mark.parametrize(
        "setting", ["threshold: 1", "threshold: 0.001", "patience: 1", "patience: 10"]
    )
    def test_the_edges_of_what_is_allowed_are_allowed(self, setting: str) -> None:
        parse_config(self.settings(f"  {setting}\n"), {})  # does not raise

    @given(thousandths=st.integers(min_value=1, max_value=1_000))
    def test_every_score_above_nothing_and_up_to_certainty_is_a_threshold(
        self, thousandths: int
    ) -> None:
        threshold = thousandths / 1_000

        config = parse_config(self.settings(f"  threshold: {threshold}\n"), {})

        assert config.listening.threshold == threshold

    @given(thousandths=st.integers(min_value=1_001, max_value=100_000))
    def test_and_nothing_over_certainty_is(self, thousandths: int) -> None:
        with pytest.raises(ConfigError, match=r"listening\.threshold"):
            parse_config(self.settings(f"  threshold: {thousandths / 1_000}\n"), {})

    def test_the_folder_of_the_models_is_the_home_of_whoever_wrote_it_when_it_begins_with_a_tilde(
        self,
    ) -> None:
        listening = parse_config(self.settings("  models_dir: ~/models\n"), {}).listening

        assert listening.models_dir == Path.home() / "models"

    def test_a_folder_is_left_as_it_was_written(self) -> None:
        listening = parse_config(self.settings("  models_dir: /srv/models\n"), {}).listening

        assert listening.models_dir == Path("/srv/models")


class TestWhatCannotBe:
    @pytest.mark.parametrize(
        ("text", "named"),
        [
            (MINIMAL + "audio:\n  volum: 5\n", "audio.volum"),
            (MINIMAL + "audio:\n  volume: 150\n", "audio.volume"),
            (MINIMAL + "audio:\n  volume_step: 0\n", "audio.volume_step"),
            (MINIMAL + "audio:\n  volume_step: 101\n", "audio.volume_step"),
            (MINIMAL + "edge:\n  url: http://edge\n", "edge.url"),
            (MINIMAL + "listening:\n  mode: shouting\n", "listening.mode"),
            (MINIMAL + "listening:\n  silence_seconds: 0\n", "listening.silence_seconds"),
            (MINIMAL + "reconnect:\n  first_s: 10\n  max_s: 5\n", "reconnect"),
            (MINIMAL + "log_level: LOUD\n", "log_level"),
            ("device:\n  token: x\n", "device.hardware_id"),
        ],
    )
    def test_a_wrong_setting_is_named(self, text: str, named: str) -> None:
        with pytest.raises(ConfigError) as error:
            parse_config(text, {})

        assert named in str(error.value)

    def test_the_longest_wait_may_be_as_long_as_the_first(self) -> None:
        config = parse_config(MINIMAL + "reconnect:\n  first_s: 5\n  max_s: 5\n", {})

        assert (config.reconnect.first_s, config.reconnect.max_s) == (5.0, 5.0)

    def test_a_longest_wait_that_is_shorter_than_the_first_says_so(self) -> None:
        with pytest.raises(ConfigError, match="max_s must not be shorter than first_s"):
            parse_config(MINIMAL + "reconnect:\n  first_s: 10\n  max_s: 5\n", {})

    def test_it_is_said_by_the_name_of_the_setting_and_never_by_its_value(self) -> None:
        with pytest.raises(ConfigError) as error:
            parse_config(MINIMAL + "edge:\n  url: http://a-credential-in-a-url@edge\n", {})

        assert "a-credential-in-a-url" not in str(error.value)

    @pytest.mark.parametrize("capability", ["display", "led", "media_player", "sensors"])
    def test_a_capability_with_no_hands_here_is_refused(self, capability: str) -> None:
        text = MINIMAL + f"  capabilities: [audio_out, {capability}]\n"

        with pytest.raises(ConfigError, match=f"no hands for {capability}"):
            parse_config(text, {})

    def test_a_device_that_can_do_nothing_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="can do nothing"):
            parse_config(MINIMAL + "  capabilities: []\n", {})

    @pytest.mark.parametrize("text", ["", "- a list\n- of things\n", "just words", "42"])
    def test_a_file_that_is_not_a_mapping_of_settings_is_refused(self, text: str) -> None:
        with pytest.raises(ConfigError, match="mapping of settings"):
            parse_config(text, {})

    def test_a_file_that_is_not_yaml_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="not YAML"):
            parse_config("device: [unclosed\n", {})

    def test_a_section_that_is_not_a_mapping_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="each a mapping"):
            parse_config("device: kitchen-01\n", {})

    def test_a_section_that_is_left_empty_is_the_defaults_of_it(self) -> None:
        config = parse_config(MINIMAL + "audio:\nlistening:\n", {})

        assert config.audio.volume == 100


class TestWhereTheFileIs:
    def test_a_file_is_read_from_where_it_is(self, tmp_path: Path) -> None:
        path = tmp_path / "device.yaml"
        path.write_bytes(MINIMAL.encode("utf-8"))

        assert load_config(path, {}).device.hardware_id == "kitchen-01"

    def test_a_file_that_is_not_there_says_how_to_make_one(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="init"):
            load_config(tmp_path / "device.yaml", {})

    def test_a_file_that_cannot_be_read_is_an_error_that_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="cannot be read"):
            load_config(tmp_path, {})  # a folder

    def test_the_variable_says_where_it_is_before_anything_else_does(self) -> None:
        found = default_path({CONFIG_VARIABLE: "/etc/factotum/device.yaml"}, "win32")

        assert found == Path("/etc/factotum/device.yaml")

    def test_on_windows_it_is_in_the_settings_of_the_person_who_runs_it(self) -> None:
        found = default_path({"APPDATA": "C:/Users/me/AppData/Roaming"}, "win32")

        assert found == Path("C:/Users/me/AppData/Roaming/factotum/device.yaml")

    def test_elsewhere_it_is_in_the_config_folder_the_system_names(self) -> None:
        found = default_path({"XDG_CONFIG_HOME": "/home/me/.cfg"}, "linux")

        assert found == Path("/home/me/.cfg/factotum/device.yaml")

    def test_or_in_dot_config_of_the_home_folder(self) -> None:
        found = default_path({}, "linux")

        assert found == Path.home() / ".config" / "factotum" / "device.yaml"
