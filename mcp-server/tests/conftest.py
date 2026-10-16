import os
import sys

from starlette.testclient import TestClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main

_client = None


def shared_client() -> TestClient:
    """One TestClient for the whole run: the session manager's lifespan can only start once per process."""
    global _client
    if _client is None:
        _client = TestClient(main.app)
        _client.__enter__()
    return _client


def pytest_sessionfinish(session, exitstatus):
    if _client is not None:
        _client.__exit__(None, None, None)
