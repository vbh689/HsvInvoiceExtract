import importlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["MOCK_MODE"] = "true"

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    """Each test gets its own SQLite file so requests/keys/cache never leak
    across tests. app.main builds `settings`/the db connection at import
    time, so isolating a test means reloading it after the env is set.
    """
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MOCK_MODE", "true")
    # Settings reads the repo's local .env (gitignored, developer-owned) --
    # pin auth-relevant values so a developer's local dev convenience
    # (e.g. API_KEY_REQUIRED=false) can't silently change test behavior.
    monkeypatch.setenv("API_KEY_REQUIRED", "true")

    import app.main as main_module

    importlib.reload(main_module)
    return main_module


@pytest.fixture
def client(app_module):
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture
def api_key(client) -> str:
    from app.db import create_api_key

    key = create_api_key(client.app.state.db, "test-key")
    return key["plaintext"]


@pytest.fixture
def auth_client(client, api_key):
    client.headers.update({"X-API-Key": api_key})
    return client


@pytest.fixture
def sample_jpeg_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (40, 30), color=(200, 200, 200))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def sample_invoice_png_bytes() -> bytes:
    return (REPO_ROOT / "tests" / "fixtures" / "images" / "sample_invoice.png").read_bytes()
