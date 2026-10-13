"""The sound hardware that Factotum clients share: a microphone that is watched by a key or by a
wake word (an engine that needs no key: openWakeWord, or Porcupine, which does), a speaker that
takes the voice of an answer as it arrives, and where a person's speech ends. What a client
does with them is the core's (``factotum.client_core``): this is what plugs into its ports
(``VoiceInput``, ``VoiceOutput``, ``VoiceSink``).
"""

from factotum.client_audio.backend import (
    AudioBackend,
    AudioDevice,
    AudioUnavailableError,
    DeviceChoice,
    SoundDeviceBackend,
)
from factotum.client_audio.endpoint import capture_speech
from factotum.client_audio.inputs import PushToTalk, WakeWord
from factotum.client_audio.keys import EnterKey
from factotum.client_audio.microphone import Microphone
from factotum.client_audio.speaker import DEFAULT_VOLUME_STEP, Speaker, gain_of
from factotum.client_audio.wakeword import (
    ACCESS_KEY_VARIABLE,
    DEFAULT_OPENWAKEWORD,
    DEFAULT_WAKE_WORD,
    THRESHOLD,
    WAKE_WORDS,
    Engine,
    ModelFile,
    ModelsNotThereError,
    OpenWakeWordEngine,
    PorcupineEngine,
    WakeWordEngine,
    WakeWordError,
    WakeWordModels,
    default_models_directory,
    default_word,
    open_wake_word_engine,
    porcupine_engine,
    wake_word_engine,
)

__all__ = [
    "ACCESS_KEY_VARIABLE",
    "DEFAULT_OPENWAKEWORD",
    "DEFAULT_VOLUME_STEP",
    "DEFAULT_WAKE_WORD",
    "THRESHOLD",
    "WAKE_WORDS",
    "AudioBackend",
    "AudioDevice",
    "AudioUnavailableError",
    "DeviceChoice",
    "Engine",
    "EnterKey",
    "Microphone",
    "ModelFile",
    "ModelsNotThereError",
    "OpenWakeWordEngine",
    "PorcupineEngine",
    "PushToTalk",
    "SoundDeviceBackend",
    "Speaker",
    "WakeWord",
    "WakeWordEngine",
    "WakeWordError",
    "WakeWordModels",
    "capture_speech",
    "default_models_directory",
    "default_word",
    "gain_of",
    "open_wake_word_engine",
    "porcupine_engine",
    "wake_word_engine",
]
