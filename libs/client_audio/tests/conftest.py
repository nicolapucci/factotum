"""What every test of this folder is spared: the home of whoever runs it."""

import pytest


@pytest.fixture(autouse=True)
def _a_home_with_nothing_in_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The models of a wake word are looked for in the home of whoever runs the tests
    (``~/.cache/factotum/openwakeword``), and a machine that has fetched them would pass a test that
    one that has not would fail: what a unit test finds must not depend on it. A test that wants a
    folder of its own gives it (``models_dir``), or says where the home is. (``Path.home()`` and
    ``~`` in a path both read these variables, which is why they are what is set.)"""
    home = str(tmp_path_factory.mktemp("home"))
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
