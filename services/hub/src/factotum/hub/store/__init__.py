"""The store of the hub: the contract, and the two ways to keep what it keeps."""

from factotum.hub.store.base import HubStore, StoredUser
from factotum.hub.store.memory import InMemoryStore

__all__ = ["HubStore", "InMemoryStore", "StoredUser"]
