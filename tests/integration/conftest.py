"""Integration tests: real external systems (a database, an upstream API, a container).

Mark every test here `@pytest.mark.integration`. They are skipped unless
RUN_INTEGRATION=1, so `pytest` stays fast and offline; CI runs them in a job of
their own once there are any.
"""

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="set RUN_INTEGRATION=1 to run integration tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
