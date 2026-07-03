import importlib


def test_tailor_package_imports():
    assert importlib.import_module("src.tailor") is not None
