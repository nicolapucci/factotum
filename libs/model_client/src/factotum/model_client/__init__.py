"""How the Factotum services reach the model gateway: chat (whole or streamed), embeddings, speech
and the profile of a role, always acting for an owner."""

from factotum.model_client.chat import Chat, GatewayChat
from factotum.model_client.embedder import (
    DEFAULT_BATCH,
    PROBE,
    Embedded,
    Embedder,
    GatewayEmbedder,
)
from factotum.model_client.gateway import (
    Delegation,
    DevDelegation,
    HubDelegation,
    delegation_for,
    error_from,
)
from factotum.model_client.speech import GatewaySpeech, Speech

__all__ = [
    "DEFAULT_BATCH",
    "PROBE",
    "Chat",
    "Delegation",
    "DevDelegation",
    "Embedded",
    "Embedder",
    "GatewayChat",
    "GatewayEmbedder",
    "GatewaySpeech",
    "HubDelegation",
    "Speech",
    "delegation_for",
    "error_from",
]
