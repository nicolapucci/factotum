"""The `factotum` namespace is shared by several distributions; make sure they coexist."""

import importlib

import pytest


@pytest.mark.parametrize("module", ["contracts", "common", "devices"])
def test_libraries_share_the_factotum_namespace(module: str) -> None:
    imported = importlib.import_module(f"factotum.{module}")

    assert imported.__name__ == f"factotum.{module}"


def test_factotum_is_a_namespace_package() -> None:
    import factotum

    # A regular package has a __file__; a PEP 420 namespace package does not.
    assert getattr(factotum, "__file__", None) is None
