"""SQL migrations: numbered files, applied in order, each once, and never edited afterwards.

A service keeps its migrations next to its code as ``0001_name.sql``, ``0002_name.sql``... and its
tables in a schema of its own. The runner records what it applied, with a checksum, in
``<schema>.schema_migrations``, so an edited migration is noticed instead of silently diverging from
the databases that already ran it. Migrations only go forward.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from factotum.contracts.errors import FactotumError
from factotum.db.types import Connection

_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_SCHEMA = re.compile(r"^[a-z_][a-z0-9_]*$")


class MigrationError(FactotumError):
    code = "migration_error"


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def load_migrations(directory: Path) -> list[Migration]:
    """The migrations in ``directory``, in order. The numbers must run 1, 2, 3... without gaps."""
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        matched = _FILE.match(path.name)
        if matched is None:
            raise MigrationError(f"{path.name}: a migration is named 0001_some_name.sql")
        found.append(Migration(int(matched.group(1)), matched.group(2), path.read_text("utf-8")))
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"migration {migration.version:04d} is where {expected:04d} was expected: the "
                "numbers must run 1, 2, 3... without gaps or repeats"
            )
    return found


async def migrate(
    connection: Connection,
    schema: str,
    migrations: Sequence[Migration],
) -> list[int]:
    """Bring ``schema`` up to date. Returns the versions applied by this call.

    Only one runner works at a time: two service replicas starting together do not race.
    """
    if not _SCHEMA.match(schema):
        raise MigrationError(f"{schema!r} is not a usable schema name")
    await connection.execute("SELECT pg_advisory_lock(hashtext($1))", f"factotum.migrate.{schema}")
    try:
        await connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await connection.execute(
            f'CREATE TABLE IF NOT EXISTS "{schema}".schema_migrations ('
            "version integer PRIMARY KEY, name text NOT NULL, checksum text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        rows = await connection.fetch(
            f'SELECT version, checksum FROM "{schema}".schema_migrations'  # noqa: S608 - validated
        )
        applied = {row["version"]: row["checksum"] for row in rows}

        known = {migration.version for migration in migrations}
        ahead = sorted(set(applied) - known)
        if ahead:
            raise MigrationError(
                f"the database has migration {ahead[-1]:04d}, which this code does not know: "
                "it is newer than the service"
            )

        done: list[int] = []
        for migration in migrations:
            if migration.version in applied:
                if applied[migration.version] != migration.checksum:
                    raise MigrationError(
                        f"migration {migration.version:04d}_{migration.name} was edited after it "
                        "was applied: add a new migration instead"
                    )
                continue
            async with connection.transaction():
                await connection.execute(migration.sql)
                await connection.execute(
                    f'INSERT INTO "{schema}".schema_migrations (version, name, checksum) '  # noqa: S608
                    "VALUES ($1, $2, $3)",
                    migration.version,
                    migration.name,
                    migration.checksum,
                )
            done.append(migration.version)
        return done
    finally:
        await connection.execute(
            "SELECT pg_advisory_unlock(hashtext($1))", f"factotum.migrate.{schema}"
        )
