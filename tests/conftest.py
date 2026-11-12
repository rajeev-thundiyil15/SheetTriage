import pytest

import app as web


@pytest.fixture(autouse=True)
def generous_run_limit(monkeypatch):
    """Most tests run several files from the same address; the limit tests set their own."""
    monkeypatch.setattr(web, "FREE_RUNS_PER_DAY", 1000)
