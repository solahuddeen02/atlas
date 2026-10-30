import importlib

import pytest


@pytest.mark.parametrize("name", ["atlas", "atlas.kernel", "atlas.sdk", "modules"])
def test_package_imports(name: str) -> None:
    assert importlib.import_module(name).__doc__
