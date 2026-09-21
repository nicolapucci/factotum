"""Configuration of the conversation service, read from the environment.

The database is the one of ``DatabaseSettings`` (``FACTOTUM_POSTGRES_*``, the names compose uses to
create it). Nothing here names a model, a provider or a key: those are the gateway's, and the
documents are the knowledge service's.
"""

from datetime import timedelta
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, model_validator

from factotum.conversation.turn import TurnSettings
from factotum.db.settings import DatabaseSettings


class ConversationSettings(DatabaseSettings):
    conversation_host: str = "127.0.0.1"
    """The interface to listen on. Loopback unless told otherwise: the service is for the other
    services and is not published outside the deployment."""

    conversation_port: int = Field(default=8083, ge=1, le=65535)

    conversation_gateway_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8081")
    """Where the model gateway is: it is asked for every answer and every embedding."""

    conversation_knowledge_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8082")
    """Where the knowledge service is: it is asked for the passages that answer a message."""

    conversation_actions_url: AnyHttpUrl | None = None
    """Where the actions service is: it is asked, before the documents, whether a message asks for
    something to be done. Without one no message is taken to ask for anything, and every message is
    a question."""

    conversation_actions_timeout_s: float = Field(default=120.0, gt=0, le=600)
    """How long the actions service is waited for. It is the whole of an action (reading the
    message, asking for an approval, running the skill), so it is as long as the model's."""

    conversation_timezone: str = "UTC"
    """The time zone of the household (``Europe/Rome``): "yesterday" and "last week" are counted in
    it, and the date the model is told is the one there."""

    conversation_session_minutes: int = Field(default=30, ge=1, le=24 * 60)
    """How long a conversation stays the one a message that names none belongs to."""

    conversation_shared_window_seconds: int = Field(default=60, ge=1, le=3600)
    """How far back a shared device sees what was said on it. A shared device has no history of its
    own (anybody may be speaking to it), only this window before a message, in which whoever said it
    is most likely still there; what is said on it is forgotten after twice as long."""

    conversation_recent_exchanges: int = Field(default=3, ge=1, le=10)
    """How many of the last exchanges a continuation ("and in English?") brings back."""

    conversation_history_hits: int = Field(default=3, ge=1, le=10)
    """How many exchanges a reference to something said earlier brings back."""

    conversation_document_passages: int = Field(default=6, ge=1, le=20)
    """How many passages of the documents a turn asks for."""

    conversation_knowledge_timeout_s: float = Field(default=4.0, gt=0, le=60)
    """How long the documents are waited for before the answer goes on without them."""

    conversation_gate_timeout_s: float = Field(default=8.0, gt=0, le=60)
    """How long the small model is waited for to tell whether a message refers to the past. One
    that is not read in time is answered without any of it."""

    conversation_answer_timeout_s: float = Field(default=120.0, gt=0, le=600)
    """How long the model may be silent while it answers."""

    conversation_history_min_similarity: float = Field(default=0.5, ge=0, le=1)
    """How near, by meaning, an earlier exchange must be to a search to come back when its words are
    not the ones looked for. Models differ in how near unrelated texts are: tune it to yours."""

    conversation_retention_days: int = Field(default=0, ge=0, le=36500)
    """How many days what was said is kept. 0 keeps it until somebody forgets it."""

    conversation_embed_batch: int = Field(default=16, ge=1, le=64)
    """Exchanges embedded in one request to the gateway, in the background."""

    conversation_keeper_seconds: float = Field(default=5.0, gt=0, le=600)
    """How often the background work looks for exchanges that have no vector."""

    @model_validator(mode="after")
    def _the_time_zone_exists(self) -> Self:
        try:
            ZoneInfo(self.conversation_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"{self.conversation_timezone!r} is not a time zone (try Europe/Rome or UTC)"
            ) from None
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.conversation_timezone)

    @property
    def retention(self) -> timedelta | None:
        return timedelta(days=self.conversation_retention_days) or None

    @property
    def shared_keep(self) -> timedelta:
        """How long what was said on a shared device is kept: nothing looks further back than the
        window, so twice it is more than enough."""
        return timedelta(seconds=self.conversation_shared_window_seconds) * 2

    @property
    def turn(self) -> TurnSettings:
        return TurnSettings(
            recent_exchanges=self.conversation_recent_exchanges,
            history_hits=self.conversation_history_hits,
            document_passages=self.conversation_document_passages,
            knowledge_timeout_s=self.conversation_knowledge_timeout_s,
            actions_timeout_s=self.conversation_actions_timeout_s,
            session_gap=timedelta(minutes=self.conversation_session_minutes),
            shared_window=timedelta(seconds=self.conversation_shared_window_seconds),
        )
