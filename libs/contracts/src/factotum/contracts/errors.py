"""Typed errors shared by the services.

Callers react to the *kind* of error, never to a provider- or transport-specific detail. Each error
carries a stable machine-readable ``code`` so it can cross an HTTP or WebSocket boundary.
"""

from collections.abc import Mapping
from typing import Any, ClassVar


class FactotumError(Exception):
    """Base class of every error the services raise on purpose."""

    code: ClassVar[str] = "factotum_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class AuthError(FactotumError):
    """The caller could not be authenticated."""

    code = "auth_error"


class PolicyViolationError(FactotumError):
    """The request is refused by a policy (a tenant setting, a device policy...)."""

    code = "policy_violation"


class PasswordChangeRequiredError(FactotumError):
    """The caller is authenticated, but the hub refuses everything but changing the password of a
    bootstrap account until it has been changed."""

    code = "password_change_required"


class UrlNotAllowedError(FactotumError):
    """An outbound URL is refused by the SSRF guard."""

    code = "url_not_allowed"


class CapabilityNotSupportedError(FactotumError):
    """A device was asked to do something it did not declare it can do."""

    code = "capability_not_supported"


class ProviderError(FactotumError):
    """A model provider could not do what was asked.

    ``retryable`` says whether asking again may help. Callers react to the kind of error, never to
    what a particular provider happened to say.
    """

    code = "provider_error"
    retryable: ClassVar[bool] = False


class ProviderAuthError(ProviderError):
    """The provider rejected the key, or the key may not use the model."""

    code = "provider_auth_error"


class RateLimitedError(ProviderError):
    code = "rate_limited"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        retry_after_s: float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after_s = retry_after_s


class ProviderUnavailableError(ProviderError):
    """The provider is down, overloaded or unreachable."""

    code = "provider_unavailable"
    retryable = True


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    retryable = True


class ContextTooLongError(ProviderError):
    """The request does not fit in the model's context window."""

    code = "context_too_long"


class InvalidRequestError(ProviderError):
    """The provider refused the request as malformed (an unknown model, an unsupported option)."""

    code = "invalid_request"


class ContentFilteredError(ProviderError):
    """The provider refused to answer, or blocked the prompt."""

    code = "content_filtered"


class InvalidStructuredOutputError(FactotumError):
    """The model kept answering with something that does not match the requested JSON schema."""

    code = "invalid_structured_output"


class InvalidSchemaError(FactotumError):
    """The JSON schema in a request is not a valid schema."""

    code = "invalid_schema"


class BindingNotFoundError(FactotumError):
    """No model is bound to the role for this user or tenant."""

    code = "binding_not_found"


class SecretNotFoundError(FactotumError):
    """A binding or a skill refers to a secret that is not available. The message names it, never
    a value."""

    code = "secret_not_found"


class RoleNotSupportedError(FactotumError):
    """The provider bound to a role does not offer it (Anthropic has no embeddings API)."""

    code = "role_not_supported"


class NotFoundError(FactotumError):
    """Something that was asked for does not exist, or is not visible to the caller: the two look
    the same, so that a caller cannot learn what exists."""

    code = "not_found"


class CollectionNotFoundError(NotFoundError):
    code = "collection_not_found"


class DocumentNotFoundError(NotFoundError):
    code = "document_not_found"


class ConversationNotFoundError(NotFoundError):
    code = "conversation_not_found"


class ConflictError(FactotumError):
    """The request clashes with what already exists (a name in use, a document already there)."""

    code = "conflict"


class UnsupportedDocumentError(FactotumError):
    """A document that cannot be read: an unknown format, no text in it, encrypted or damaged."""

    code = "unsupported_document"


class DocumentTooLargeError(FactotumError):
    """A document over the limits on size, pages or text."""

    code = "document_too_large"


class EmbeddingNotConfiguredError(FactotumError):
    """The owner has no embedding model bound, so nothing can be indexed or searched by meaning."""

    code = "embedding_not_configured"


class ChatNotConfiguredError(FactotumError):
    """The owner has no chat model bound, so nothing can be answered."""

    code = "chat_not_configured"


class SttNotConfiguredError(FactotumError):
    """The owner has no speech-to-text model bound, so a recording cannot be understood."""

    code = "stt_not_configured"


class TtsNotConfiguredError(FactotumError):
    """The owner has no text-to-speech model bound, so an answer cannot be spoken."""

    code = "tts_not_configured"


class GatewayError(FactotumError):
    """The model gateway answered with an error the caller cannot fix by asking again."""

    code = "gateway_error"
    retryable: ClassVar[bool] = False


class KnowledgeUnavailableError(FactotumError):
    """The knowledge service could not be reached, or did not answer with a search result."""

    code = "knowledge_unavailable"
    retryable: ClassVar[bool] = True


class GatewayUnavailableError(GatewayError):
    """The model gateway could not be reached, or is failing for a while: asking again may help."""

    code = "gateway_unavailable"
    retryable = True


def error_class_for(code: str) -> type[FactotumError] | None:
    """The typed error with this stable ``code``, if there is one: what lets a service raise, as
    what it was, an error that another service answered with."""
    pending: list[type[FactotumError]] = [FactotumError]
    while pending:
        kind = pending.pop()
        if kind.code == code:
            return kind
        pending.extend(kind.__subclasses__())
    return None


class ConversationUnavailableError(FactotumError):
    """The conversation service could not be reached, or did not answer with a turn."""

    code = "conversation_unavailable"
    retryable: ClassVar[bool] = True


class DeviceNotFoundError(NotFoundError):
    """A device that is not registered, or that the caller may not see."""

    code = "device_not_found"


class SkillInvalidError(FactotumError):
    """A skill that cannot be installed: its ``SKILL.md`` is not one, or what it declares is not
    valid. The message says where and why, never what was written."""

    code = "skill_invalid"


class SkillNotFoundError(NotFoundError):
    """A skill that is not installed, or that the caller may not see."""

    code = "skill_not_found"


class ActionsUnavailableError(FactotumError):
    """The actions service could not be reached, or did not answer with an outcome."""

    code = "actions_unavailable"
    retryable: ClassVar[bool] = True


class SkillRunError(FactotumError):
    """A skill was run and did not work: its script failed, or took too long."""

    code = "skill_run_error"


class RunnerUnavailableError(SkillRunError):
    """Nothing can run skills right now."""

    code = "runner_unavailable"


class PlanError(SkillRunError):
    """A skill that is instructions could not be run: no plan that the checks accept was written
    for it. The message says what the checks said of the last one."""

    code = "plan_error"


class EgressGrantError(FactotumError):
    """What a skill presented to the egress proxy to say where it may go is not a grant that the
    runner made: it is malformed, it was altered, or it has run out. The message never says which
    part of it is wrong."""

    code = "egress_grant_invalid"


class ProtocolError(FactotumError):
    """A device sent something the protocol between devices and the edge does not allow."""

    code = "protocol_error"


class InvalidFrameError(ProtocolError):
    """A frame that is not one of the protocol: not JSON, an unknown type, a field that is wrong,
    or too big."""

    code = "invalid_frame"


class UnsupportedProtocolError(ProtocolError):
    """A device speaks a version of the protocol the edge does not."""

    code = "unsupported_protocol"
