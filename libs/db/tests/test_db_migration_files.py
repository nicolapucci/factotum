from pathlib import Path

import pytest

from factotum.db.migrations import Migration, MigrationError, load_migrations, migrate


def write(directory: Path, *names: str) -> Path:
    for name in names:
        (directory / name).write_text(f"-- {name}\nSELECT 1;\n", encoding="utf-8")
    return directory


def test_migrations_are_read_in_order(tmp_path: Path) -> None:
    write(tmp_path, "0002_add_things.sql", "0001_create_schema.sql", "0003_index_things.sql")

    loaded = load_migrations(tmp_path)

    assert [(m.version, m.name) for m in loaded] == [
        (1, "create_schema"),
        (2, "add_things"),
        (3, "index_things"),
    ]


def test_no_migrations_is_fine(tmp_path: Path) -> None:
    assert load_migrations(tmp_path) == []


@pytest.mark.parametrize(
    "names",
    [("0002_second.sql",), ("0001_first.sql", "0003_third.sql"), ("0001_a.sql", "0001_b.sql")],
    ids=["starts-at-two", "gap", "repeat"],
)
def test_the_numbers_must_run_without_gaps_or_repeats(
    tmp_path: Path, names: tuple[str, ...]
) -> None:
    write(tmp_path, *names)

    with pytest.raises(MigrationError, match="without gaps or repeats"):
        load_migrations(tmp_path)


@pytest.mark.parametrize("name", ["1_short.sql", "0001-dash.sql", "0001_Upper.sql", "0001.sql"])
def test_a_badly_named_file_is_refused(tmp_path: Path, name: str) -> None:
    write(tmp_path, name)

    with pytest.raises(MigrationError, match=r"is named 0001_some_name\.sql"):
        load_migrations(tmp_path)


def test_other_files_are_not_migrations(tmp_path: Path) -> None:
    write(tmp_path, "0001_first.sql")
    (tmp_path / "README.md").write_text("notes", encoding="utf-8")

    assert len(load_migrations(tmp_path)) == 1


def test_the_checksum_changes_with_the_sql() -> None:
    first = Migration(1, "a", "SELECT 1")

    assert first.checksum == Migration(1, "a", "SELECT 1").checksum
    assert first.checksum != Migration(1, "a", "SELECT 2").checksum
    assert len(first.checksum) == 64


@pytest.mark.parametrize("schema", ["Bad", "with space", "1abc", 'quo"te', "a;b", ""])
async def test_a_schema_name_that_could_be_an_injection_is_refused_before_anything_runs(
    schema: str,
) -> None:
    class NoConnection:
        """Any use of it would fail the test: the name must be refused first."""

    with pytest.raises(MigrationError, match="not a usable schema name"):
        await migrate(NoConnection(), schema, [])  # type: ignore[arg-type]
