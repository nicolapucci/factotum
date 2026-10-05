"""The tests of the rig itself use no service and no store: they run once, and not once for each
place the services keep what they keep (the fixture of ``e2e/tests/conftest.py`` by this name)."""

import pytest


@pytest.fixture(autouse=True)
def _where_the_services_keep_what_they_keep() -> None:
    return None
