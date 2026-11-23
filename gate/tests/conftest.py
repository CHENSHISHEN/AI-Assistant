import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

TOKEN = "test-token-0123456789"


@pytest.fixture
def settings():
    return Settings(token=TOKEN, orca_environment="assistant-gate", bind_host="127.0.0.1")


@pytest.fixture
def make_client(settings):
    def _make(orca=None):
        return TestClient(create_app(settings, orca))
    return _make


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {TOKEN}"}
