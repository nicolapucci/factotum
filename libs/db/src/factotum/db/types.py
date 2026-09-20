"""The type of a database connection, as the queries are written against it."""

from typing import TYPE_CHECKING

import asyncpg
from asyncpg.pool import PoolConnectionProxy

if TYPE_CHECKING:
    Connection = asyncpg.Connection[asyncpg.Record] | PoolConnectionProxy[asyncpg.Record]
    """A connection, straight or borrowed from a pool."""
else:  # asyncpg's classes are subscriptable only for the type checker
    Connection = asyncpg.Connection | PoolConnectionProxy
