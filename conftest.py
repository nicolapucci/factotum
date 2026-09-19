"""Test configuration shared by every library and service."""

from hypothesis import settings

# Property tests build pydantic models, which is quick, but a fixed deadline only makes them flaky
# on a busy machine.
settings.register_profile("factotum", deadline=None, max_examples=100)
settings.load_profile("factotum")
