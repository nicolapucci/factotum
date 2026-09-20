"""The store of conversations: the contract, and the two ways to keep them."""

from factotum.conversation.store.base import ConversationStore, Found, Pending, Vector
from factotum.conversation.store.memory import InMemoryStore

__all__ = ["ConversationStore", "Found", "InMemoryStore", "Pending", "Vector"]
