"""Models and providers as the rest of the system sees them.

Services ask for a *role*, never for a model. A user (or the tenant) binds each role to a
*binding*: which provider, at which address, with which key and model. Only the model gateway reads
bindings; everybody else gets, at most, the :class:`Profile` of a role, which says what the role can
do without saying what it is.
"""

import re
from enum import StrEnum
from typing import Self
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from factotum.contracts.base import Contract
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import ModelBindingId, TenantId

_SECRET_REF = re.compile(r"^env:[A-Z][A-Z0-9_]{0,63}$")
_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}$")


class ModelRole(StrEnum):
    """What a service needs a model for."""

    CHAT_MAIN = "chat.main"
    """The model that writes answers."""

    CHAT_FAST = "chat.fast"
    """A quicker, cheaper model for routing and extraction; falls back to ``chat.main``."""

    EMBEDDING = "embedding"
    STT = "stt"
    TTS = "tts"
    RERANK = "rerank"
    WEB_SEARCH = "web_search"

    @property
    def is_chat(self) -> bool:
        return self in {ModelRole.CHAT_MAIN, ModelRole.CHAT_FAST}


class ProviderKind(StrEnum):
    """One adapter per kind."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    """Any server that speaks the OpenAI API: vLLM, LM Studio, llama.cpp, OpenRouter, Groq..."""

    WHISPER_LOCAL = "whisper_local"
    """``faster-whisper``, in the gateway's own process: speech-to-text only, no address, no key."""

    PIPER_LOCAL = "piper_local"
    """``piper``, in the gateway's own process: text-to-speech only, no address, no key."""


HOSTED_PROVIDERS = frozenset({ProviderKind.OPENAI, ProviderKind.ANTHROPIC, ProviderKind.GEMINI})
"""Providers that always need a key and have a well-known address."""

ADDRESS_REQUIRED = frozenset({ProviderKind.OLLAMA, ProviderKind.OPENAI_COMPATIBLE})
"""Providers with no fixed address: the user must say where the server is."""


class StructuredOutputSupport(StrEnum):
    NATIVE = "native"
    """The provider constrains the output to a JSON schema itself."""

    EMULATED = "emulated"
    """The gateway asks for JSON in the prompt, validates it and retries."""


class BindingOptions(Contract):
    """Per-binding settings the user may declare. Every value has a prudent default."""

    max_input_tokens: int = Field(default=8192, ge=256)
    """The context the caller may fill. Declared by the user: model catalogues change too often to
    be hardcoded, and a wrong guess overflows the window or wastes it."""

    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout_s: float = Field(default=60.0, gt=0, le=600)
    native_structured_output: bool | None = None
    """None means the provider's default; set it to false for a model that lacks the feature."""

    num_ctx: int | None = Field(default=None, ge=256)
    """Ollama only: the context window to request, prompt and answer together. Defaults to
    ``max_input_tokens`` plus room for the answer: leaving it unset lets the server pick a small
    window and truncate the prompt silently."""

    dimensions: int | None = Field(default=None, ge=1)
    """Embeddings only: ask for vectors of this size, where the provider supports it."""

    stream_usage: bool | None = None
    """OpenAI-compatible servers: ask for token usage in streams (not every server accepts it)."""

    languages: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=8)
    """Speech-to-text only: the languages people speak to the device, as the codes Whisper uses
    (``it``, ``en``). One is that language, several limit the guess to them, and unset leaves the
    model to guess among all of its own: for a short command ("apri il garage") that is often a
    language nobody spoke, and what it writes is then in that language, or in its script."""

    @field_validator("languages")
    @classmethod
    def _languages_are_distinct_codes(
        cls, languages: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if languages is None:
            return None
        for code in languages:
            if not _LANGUAGE_CODE.fullmatch(code):
                raise ValueError(f"{code!r} is not a language code such as 'it' or 'en'")
        if len(set(languages)) != len(languages):
            raise ValueError("a language is listed twice")
        return languages


class ModelBinding(Contract):
    """A role bound to a model at a provider, by a user or by the tenant."""

    # A key pasted where a reference belongs must not end up in an error message or a log.
    model_config = ConfigDict(
        frozen=True, extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

    id: ModelBindingId
    tenant_id: TenantId
    owner: Owner
    role: ModelRole
    provider: ProviderKind
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    secret_ref: str | None = None
    """Where the key lives (``env:OPENAI_API_KEY``), never the key itself."""

    options: BindingOptions = Field(default_factory=BindingOptions)

    @field_validator("owner")
    @classmethod
    def _owned_by_a_user_or_the_tenant(cls, owner: Owner) -> Owner:
        if owner.kind is OwnerKind.DEVICE:
            raise ValueError("a model binding belongs to a user or to the tenant")
        return owner

    @field_validator("base_url")
    @classmethod
    def _base_url_is_http(cls, base_url: str | None) -> str | None:
        if base_url is None:
            return None
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("base_url must be an http or https address")
        return base_url.rstrip("/")

    @field_validator("secret_ref")
    @classmethod
    def _secret_ref_has_a_known_shape(cls, secret_ref: str | None) -> str | None:
        if secret_ref is not None and not _SECRET_REF.fullmatch(secret_ref):
            raise ValueError("secret_ref must look like env:VARIABLE_NAME")
        return secret_ref

    @model_validator(mode="after")
    def _has_what_the_provider_needs(self) -> Self:
        if self.provider in ADDRESS_REQUIRED and self.base_url is None:
            raise ValueError(f"{self.provider.value} needs a base_url")
        if self.provider in HOSTED_PROVIDERS and self.secret_ref is None:
            raise ValueError(f"{self.provider.value} needs a secret_ref")
        return self


class Profile(Contract):
    """What a role can do, without saying which model does it."""

    role: ModelRole
    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    streaming: bool = True
    structured_output: StructuredOutputSupport | None = None
    """None for a role that does not produce text (embeddings, speech)."""
