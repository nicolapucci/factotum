"""Test configuration shared by every library and service."""

from pathlib import Path

import pytest
from hypothesis import settings

# Fixtures for tests that need a real Postgres (see factotum.db.testing).
pytest_plugins = ["factotum.db.testing"]

# Property tests build pydantic models, which is quick, but a fixed deadline only makes them flaky
# on a busy machine.
settings.register_profile("factotum", deadline=None, max_examples=100)
settings.load_profile("factotum")


@pytest.fixture(scope="session")
def _directory_without_an_env_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("cwd")


@pytest.fixture(autouse=True)
def _run_where_there_is_no_env_file(
    monkeypatch: pytest.MonkeyPatch, _directory_without_an_env_file: Path
) -> None:
    """Settings read a ``.env`` in the working directory, and a developer has one when the stack is
    up (it holds the database password, the key of the egress, the environment). What a unit test
    finds must not depend on it: a proxy that is told it has no key would otherwise start, and one
    that must refuse to run in production would run in development. A test that wants a file of its
    own makes one, and moves there."""
    monkeypatch.chdir(_directory_without_an_env_file)
