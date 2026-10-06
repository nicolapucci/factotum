"""What a device is told about itself: where the edge is, who it was paired as, what it is made of
and how it listens, in a file that a person writes once (``config/device.example.yaml``).

Everything in it is checked when it is read, and what is wrong is said in a line, with the name of
the setting: a device that starts and then fails on a wrong credential, or on a microphone that is
not there, is a device that nobody can tell why it is silent. The two secrets of a device, its
credential and the key of its wake-word engine, may be in the file or in the environment, which wins
(a file of settings is often a file in a repository; the credential of a device is not).
"""

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, ValidationError, model_validator

from factotum.client_audio import ACCESS_KEY_VARIABLE, DEFAULT_WAKE_WORD, DeviceChoice
from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability

DEFAULT_URL = "ws://127.0.0.1:8084/v1/connect"
TOKEN_VARIABLE = "FACTOTUM_DEVICE_TOKEN"  # noqa: S105 - the name of a variable, not a secret
URL_VARIABLE = "FACTOTUM_DEVICE_URL"
CONFIG_VARIABLE = "FACTOTUM_DEVICE_CONFIG"

HANDS = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT})
"""What this application can be: a microphone and a speaker. A device with a screen, a light or a
media player has hands that this application does not (yet), and a capability that is declared
without hands is a command that can only fail."""


class ConfigError(Exception):
    """The file of settings is not there, is not YAML, or says what cannot be."""


class Mode(StrEnum):
    PUSH_TO_TALK = "push_to_talk"
    """A key: Enter begins, and being quiet (or Enter again) ends."""

    WAKE_WORD = "wake_word"
    """A word that Porcupine watches for: it needs a Picovoice AccessKey."""


class EdgeSettings(Contract):
    url: str = Field(default=DEFAULT_URL, pattern=r"^wss?://")
    """Where the edge is: ``ws://`` on a network that is the home's, ``wss://`` behind a proxy."""


class DeviceSettings(Contract):
    hardware_id: str = Field(min_length=1, max_length=200)
    """The label this device was paired under, as the hub or the file of devices has it."""

    token: SecretStr | None = None
    """The credential it was given when it was paired: here or in ``$FACTOTUM_DEVICE_TOKEN``."""

    capabilities: frozenset[Capability] = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT})
    """What this hardware has: never more than the device is registered for, and a device that
    declares less (no microphone: ``[audio_out]``) is a speaker that is spoken through."""

    @model_validator(mode="after")
    def _is_something_this_application_can_be(self) -> "DeviceSettings":
        unsupported = sorted(c.value for c in self.capabilities - HANDS)
        if unsupported:
            raise ValueError(
                f"this application has no hands for {', '.join(unsupported)}: "
                f"a device of it is made of {', '.join(sorted(c.value for c in HANDS))}"
            )
        if not self.capabilities:
            raise ValueError("a device that can do nothing is not a device")
        return self


class AudioSettings(Contract):
    input_device: int | str | None = None
    """The microphone: its number, or a part of its name (``doctor`` lists them); the default of
    the system when it is not said."""

    output_device: int | str | None = None
    volume: int = Field(default=100, ge=0, le=100)
    """How loud it speaks at first, in percent of what the voice makes: a ``set_volume`` of a
    skill changes it, and the volume of the system is left alone."""


class ListeningSettings(Contract):
    mode: Mode = Mode.PUSH_TO_TALK
    wake_word: str = DEFAULT_WAKE_WORD
    """One of Porcupine's built-in keywords."""

    access_key: SecretStr | None = None
    """The AccessKey of the wake-word engine: here or in ``$PICOVOICE_ACCESS_KEY``."""

    silence_seconds: float = Field(default=1.2, gt=0, le=10)
    """How long a person is quiet before what they said is over."""

    silence_level: float = Field(default=150, ge=0)
    """The mean level, of 32768, under which a stretch of sound is quiet."""

    max_seconds: float = Field(default=12.0, gt=0, le=60)
    """The most that one thing said may last."""

    listen_while_answering: bool = False
    """Whether to go on listening while the answer is being said, so that a person who speaks over
    it stops it. A speaker that hears itself has to wait; headphones need not."""


class ReconnectSettings(Contract):
    first_s: float = Field(default=1.0, gt=0)
    max_s: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _the_longest_wait_is_not_shorter_than_the_first(self) -> "ReconnectSettings":
        if self.max_s < self.first_s:
            raise ValueError("max_s must not be shorter than first_s")
        return self


class Config(Contract):
    edge: EdgeSettings = EdgeSettings()
    device: DeviceSettings
    audio: AudioSettings = AudioSettings()
    listening: ListeningSettings = ListeningSettings()
    reconnect: ReconnectSettings = ReconnectSettings()
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def _what_is_needed_to_listen_is_there(self) -> "Config":
        waits_for_a_word = (
            Capability.AUDIO_IN in self.device.capabilities
            and self.listening.mode is Mode.WAKE_WORD
        )
        if waits_for_a_word and self.listening.access_key is None:
            raise ValueError(
                f"listening.mode wake_word needs an access key: listening.access_key or "
                f"${ACCESS_KEY_VARIABLE} (a free one is at https://console.picovoice.ai/)"
            )
        return self

    @property
    def token(self) -> str:
        """The credential, which the file or the environment must have given."""
        assert self.device.token is not None  # noqa: S101 - ``load`` refuses a config without
        return self.device.token.get_secret_value()

    @property
    def can_hear(self) -> bool:
        return Capability.AUDIO_IN in self.device.capabilities

    @property
    def can_speak(self) -> bool:
        return Capability.AUDIO_OUT in self.device.capabilities

    @property
    def input_device(self) -> DeviceChoice:
        return self.audio.input_device

    @property
    def output_device(self) -> DeviceChoice:
        return self.audio.output_device


def default_path(environ: Mapping[str, str], system: str) -> Path:
    """Where the file is looked for when it is not said: ``$FACTOTUM_DEVICE_CONFIG``, else the
    folder of settings of the person who runs it (``%APPDATA%`` on Windows, ``~/.config``
    elsewhere)."""
    if environ.get(CONFIG_VARIABLE):
        return Path(environ[CONFIG_VARIABLE])
    if system == "win32" and environ.get("APPDATA"):
        return Path(environ["APPDATA"]) / "factotum" / "device.yaml"
    base = environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "factotum" / "device.yaml"


def parse_config(text: str, environ: Mapping[str, str]) -> Config:
    """The settings in ``text`` (YAML), with what the environment says put over them."""
    try:
        found: Any = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"the file is not YAML: {error}") from None
    if not isinstance(found, dict):
        raise ConfigError("the file is a mapping of settings (edge, device, audio, listening...)")
    sections: dict[str, Any] = {key: (value or {}) for key, value in found.items()}
    device = sections.setdefault("device", {})
    listening = sections.setdefault("listening", {})
    edge = sections.setdefault("edge", {})
    if not all(isinstance(section, dict) for section in (device, listening, edge)):
        raise ConfigError("device, listening and edge are each a mapping of settings")
    if environ.get(TOKEN_VARIABLE):
        device["token"] = environ[TOKEN_VARIABLE]
    if environ.get(ACCESS_KEY_VARIABLE):
        listening["access_key"] = environ[ACCESS_KEY_VARIABLE]
    if environ.get(URL_VARIABLE):
        edge["url"] = environ[URL_VARIABLE]
    try:
        config = Config.model_validate(sections)
    except ValidationError as error:
        raise ConfigError(_said(error)) from None
    if config.device.token is None:
        raise ConfigError(
            f"the device has no credential: device.token, or ${TOKEN_VARIABLE} "
            "(the one it was given when it was paired)"
        )
    return config


def load_config(path: Path, environ: Mapping[str, str]) -> Config:
    """The settings in the file at ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"there is no file of settings at {path} (``factotum-device init`` writes one)"
        ) from None
    except OSError as error:
        raise ConfigError(f"{path} cannot be read: {error.strerror or error}") from None
    return parse_config(text, environ)


def _said(error: ValidationError) -> str:
    """What is wrong, a line for each setting, and never the value of a secret."""
    lines = []
    for each in error.errors():
        where = ".".join(str(part) for part in each["loc"]) or "the file"
        what = str(each["msg"]).removeprefix("Value error, ")
        lines.append(f"{where}: {what}")
    return "; ".join(lines)
