"""Shared pytest setup. Lab 4 splits the data tests by kind; see tests/test_data.py."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "contract: an assertion about the data, not the code (CI: data contract tests)")
