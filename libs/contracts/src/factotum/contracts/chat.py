"""The normalized chat API: the same request and answer whatever the provider behind it."""

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from factotum.contracts.base import Contract
from factotum.contracts.models import ModelRole


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(Contract):
    role: MessageRole
    content: str = Field(min_length=1, max_length=200_000)


class ChatRequest(Contract):
    role: ModelRole = ModelRole.CHAT_MAIN
    """The role to use. The caller never names a model."""

    messages: tuple[ChatMessage, ...] = Field(min_length=1, max_length=200)
    """System messages, then a conversation that ends with a user message."""

    response_schema: dict[str, Any] | None = None
    """A JSON Schema. When present the answer is a JSON value validated against it, returned in
    ``ChatResult.parsed``: with the provider's native mechanism where there is one, otherwise by
    asking for JSON, validating and retrying."""

    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    stream: bool = False

    @field_validator("role")
    @classmethod
    def _is_a_chat_role(cls, role: ModelRole) -> ModelRole:
        if not role.is_chat:
            raise ValueError(f"{role.value!r} is not a chat role")
        return role

    @model_validator(mode="after")
    def _is_a_coherent_conversation(self) -> Self:
        if self.messages[-1].role is not MessageRole.USER:
            raise ValueError("the last message must be a user message")
        if self.stream and self.response_schema is not None:
            raise ValueError("structured output cannot be streamed")
        return self


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    """Cut off by the output limit."""

    CONTENT_FILTER = "content_filter"
    """Refused or blocked by the provider."""

    OTHER = "other"


class Usage(Contract):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class ChatResult(Contract):
    text: str
    parsed: Any | None = None
    """The validated JSON value when a ``response_schema`` was given."""

    finish_reason: FinishReason
    usage: Usage | None = None


class TextDelta(Contract):
    model_config = ConfigDict(str_strip_whitespace=False)
    """A chunk of text is not trimmed: the space at its edge is what separates it from the next
    word, and stripping it glues the words of a streamed answer together."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class UsageEvent(Contract):
    type: Literal["usage"] = "usage"
    usage: Usage


class FinishEvent(Contract):
    type: Literal["finish"] = "finish"
    reason: FinishReason


class ErrorEvent(Contract):
    """The stream failed after it had started; the code is the one of the matching error."""

    type: Literal["error"] = "error"
    code: str
    message: str


EventUnion = TextDelta | UsageEvent | FinishEvent | ErrorEvent

ChatEvent = Annotated[EventUnion, Field(discriminator="type")]
