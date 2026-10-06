"""The sound hardware that Factotum clients share: a microphone that is watched by a key or by a
wake word, a speaker that takes the voice of an answer as it arrives, and where a person's speech
ends. What a client does with them is the core's (``factotum.client_core``): this is what plugs
into its ports (``VoiceInput``, ``VoiceOutput``, ``VoiceSink``).
"""

from factotum.client_audio.backend import (
    AudioBackend,
    AudioDevice,
    AudioUnavailableError,
    DeviceChoice,
    SoundDeviceBackend,
)
from factotum.client_audio.endpoint import capture_speech
from factotum.client_audio.inputs import (
    ACCESS_KEY_VARIABLE,
    DEFAULT_WAKE_WORD,
    PushToTalk,
    WakeWord,
)
from factotum.client_audio.keys import EnterKey
from factotum.client_audio.microphone import Microphone
from factotum.client_audio.speaker import Speaker, gain_of

__all__ = [
    "ACCESS_KEY_VARIABLE",
    "DEFAULT_WAKE_WORD",
    "AudioBackend",
    "AudioDevice",
    "AudioUnavailableError",
    "DeviceChoice",
    "EnterKey",
    "Microphone",
    "PushToTalk",
    "SoundDeviceBackend",
    "Speaker",
    "WakeWord",
    "capture_speech",
    "gain_of",
]
