"""Test configuration shared by every library and service."""

from hypothesis import settings

# Fixtures for tests that need a real Postgres (see factotum.db.testing).
pytest_plugins = ["factotum.db.testing"]

# Property tests build pydantic models, which is quick, but a fixed deadline only makes them flaky
# on a busy machine.
settings.register_profile("factotum", deadline=None, max_examples=100)
settings.load_profile("factotum")
