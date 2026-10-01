"""The normalized speech API: a recording turned into text, and text turned into a recording.

Like ``embeddings.py``, each role needs no field to say which one it is: a service asks the gateway
to transcribe or to synthesize, never for a role by name, since there is only one of each.
"""

from pydantic import Field

from factotum.contracts.audio import AudioPayload
from factotum.contracts.base import Contract
from factotum.contracts.conversation import MAX_MESSAGE_CHARS

MAX_SPEECH_CHARS = 2000
"""The limit of a ``Speech`` response part: there is no point synthesizing more of it."""


class TranscribeRequest(Contract):
    audio: AudioPayload


class TranscribeResult(Contract):
    text: str = Field(max_length=MAX_MESSAGE_CHARS)
    """What was heard. Blank when the recording had nothing recognizable in it: that is not an
    error, and is for the caller to decide what to make of."""


class SynthesizeRequest(Contract):
    text: str = Field(min_length=1, max_length=MAX_SPEECH_CHARS)


class SynthesizeResult(Contract):
    audio: AudioPayload
