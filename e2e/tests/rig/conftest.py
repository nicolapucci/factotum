"""The tests of the rig itself use no service and no store: they run once, and not once for each way
the house is run (the fixture of ``e2e/tests/conftest.py`` by this name)."""

import pytest


@pytest.fixture(autouse=True)
def _how_the_house_is_run() -> None:
    return None
