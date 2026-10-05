"""The normalized speech API: a recording turned into text, and text turned into a recording.

Like ``embeddings.py``, each role needs no field to say which one it is: a service asks the gateway
to transcribe or to synthesize, never for a role by name, since there is only one of each.

Synthesis comes back whole, as a clip, or as a stream of events (``stream: true``): what a voice has
made so far, as it is made, so that a sentence is heard before it has been all said (ADR 0023). The
events are sent as server-sent events, one JSON object each, the way a streamed chat answer is.
"""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator

from factotum.contracts.audio import (
    BYTES_PER_SAMPLE,
    MAX_CHUNK_BYTES,
    MAX_SAMPLE_RATE,
    MIN_SAMPLE_RATE,
    AudioPayload,
    StreamFormat,
    whole_samples,
)
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

    stream: bool = False
    """Answer with ``SynthesisEvent``s as the voice makes them, instead of with a clip (a
    ``SynthesizeResult``) once it has made all of it."""


class SynthesizeResult(Contract):
    audio: AudioPayload


class SynthesisFormat(Contract):
    """What the samples of the events that follow are: the first event of a stream that has any."""

    type: Literal["format"] = "format"
    format: StreamFormat = StreamFormat.PCM_S16LE
    sample_rate: int = Field(ge=MIN_SAMPLE_RATE, le=MAX_SAMPLE_RATE)


class SynthesisSamples(Contract):
    """The next piece of what the voice said: whole samples, as base64 in JSON."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    type: Literal["samples"] = "samples"
    data: bytes = Field(min_length=BYTES_PER_SAMPLE, max_length=MAX_CHUNK_BYTES)

    @field_validator("data")
    @classmethod
    def _whole_samples(cls, data: bytes) -> bytes:
        return whole_samples(data)


class SynthesisDone(Contract):
    """The voice has said it all. A stream that ends without this one or an ``error`` was cut off,
    which is not the same as finished: whoever reads it must say so."""

    type: Literal["done"] = "done"


class SynthesisError(Contract):
    """The stream failed after it had started; the code is the one of the matching error."""

    type: Literal["error"] = "error"
    code: str
    message: str


SynthesisEventUnion = SynthesisFormat | SynthesisSamples | SynthesisDone | SynthesisError

SynthesisEvent = Annotated[SynthesisEventUnion, Field(discriminator="type")]
