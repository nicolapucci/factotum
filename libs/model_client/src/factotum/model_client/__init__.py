"""How the Factotum services reach the model gateway: chat (whole or streamed), embeddings and the
profile of a role, always acting for an owner."""

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
    "HubDelegation",
    "delegation_for",
    "error_from",
]
