import importlib

import pytest
from fastapi.testclient import TestClient

from app.db import cache_get, cache_set, list_api_keys


@pytest.fixture
def dash_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpass123")
    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret-provider-key")

    import app.main as main_module

    importlib.reload(main_module)
    return main_module


@pytest.fixture
def dash_client(dash_module):
    with TestClient(dash_module.app) as client:
        yield client


@pytest.fixture
def authed_client(dash_client):
    dash_client.post("/dashboard/login", data={"password": "testpass123"})
    return dash_client


def test_unauthenticated_overview_redirects_to_login(dash_client):
    r = dash_client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/login"


def test_unauthenticated_logs_and_keys_redirect_to_login(dash_client):
    paths = ["/dashboard/logs", "/dashboard/keys", "/dashboard/extract", "/dashboard/statistics"]
    for path in paths:
        r = dash_client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard/login"


def test_statistics_export_csv_unauthenticated_redirects(dash_client):
    r = dash_client.get("/dashboard/statistics/export.csv", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/login"


def test_login_page_renders(dash_client):
    r = dash_client.get("/dashboard/login")
    assert r.status_code == 200
    assert "password" in r.text.lower()


def test_login_wrong_password_rejected(dash_client):
    r = dash_client.post("/dashboard/login", data={"password": "wrong"})
    assert r.status_code == 401

    r = dash_client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303


def test_login_correct_password_grants_access(dash_client):
    r = dash_client.post(
        "/dashboard/login", data={"password": "testpass123"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"

    r = dash_client.get("/dashboard")
    assert r.status_code == 200


def test_logout_revokes_session(authed_client):
    r = authed_client.get("/dashboard")
    assert r.status_code == 200

    r = authed_client.post("/dashboard/logout", follow_redirects=False)
    assert r.status_code == 303

    r = authed_client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/login"


def test_overview_renders(authed_client):
    r = authed_client.get("/dashboard")
    assert r.status_code == 200
    assert "Overview" in r.text or "overview" in r.text.lower()


def test_chart_data_endpoint_shape(authed_client):
    r = authed_client.get("/dashboard/api/chart-data")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {
        "bucket",
        "period",
        "requests_over_time",
        "tokens_over_time",
        "cost_over_time",
        "status_breakdown",
        "findings_by_code",
        "requests_by_key",
    }
    assert body["bucket"] in {"hour", "day", "month"}
    assert isinstance(body["requests_over_time"], list)
    assert isinstance(body["status_breakdown"], dict)


def test_statistics_page_renders(authed_client):
    r = authed_client.get("/dashboard/statistics")
    assert r.status_code == 200
    assert "Statistics" in r.text


def test_statistics_chart_data_endpoint_shape(authed_client):
    r = authed_client.get("/dashboard/api/statistics-chart-data")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {
        "bucket",
        "period",
        "requests_by_model",
        "price_basis_breakdown",
        "confidence_over_time",
        "errors_over_time",
        "cache_growth_over_time",
    }
    assert body["bucket"] in {"hour", "day", "month"}
    assert isinstance(body["requests_by_model"], list)
    assert isinstance(body["price_basis_breakdown"], dict)


def test_statistics_export_csv_returns_one_row_per_key(
    authed_client, dash_client, sample_jpeg_bytes
):
    authed_client.post(
        "/dashboard/extract",
        data={"fixture_name": "pretax_basic"},
        files={"file": ("invoice.jpg", sample_jpeg_bytes, "image/jpeg")},
    )

    r = authed_client.get("/dashboard/statistics/export.csv?period=all")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]

    lines = r.text.strip().splitlines()
    assert lines[0] == (
        "api_key_label,api_key_id,request_count,avg_confidence,"
        "total_cost_usd,avg_latency_ms,cache_hit_rate"
    )
    assert len(lines) == 2
    assert "dashboard-test" in lines[1]


def test_logs_page_renders(authed_client):
    r = authed_client.get("/dashboard/logs")
    assert r.status_code == 200


def test_log_detail_404_for_unknown_request(authed_client):
    r = authed_client.get("/dashboard/logs/does-not-exist")
    assert r.status_code == 404


def test_create_and_list_api_key(authed_client):
    r = authed_client.post("/dashboard/keys", data={"label": "warehouse-1"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/keys"

    r = authed_client.get("/dashboard/keys")
    assert r.status_code == 200
    assert "warehouse-1" in r.text
    assert "flash-key" in r.text  # plaintext key shown once, right after creation

    # a second visit must not show the plaintext key again
    r = authed_client.get("/dashboard/keys")
    assert "flash-key" not in r.text


def test_revoke_api_key(authed_client, dash_client):
    authed_client.post("/dashboard/keys", data={"label": "to-revoke"})
    key_id = list_api_keys(dash_client.app.state.db)[0]["id"]

    r = authed_client.post(f"/dashboard/keys/{key_id}/revoke", follow_redirects=False)
    assert r.status_code == 303

    r = authed_client.get(f"/dashboard/keys/{key_id}")
    assert r.status_code == 200
    assert "revoked" in r.text.lower()


def test_cache_purge_clears_all_entries(authed_client, dash_client):
    db = dash_client.app.state.db
    cache_set(db, "cache-key-1", {"a": 1})
    cache_set(db, "cache-key-2", {"b": 2})

    r = authed_client.post("/dashboard/cache/purge", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/statistics"

    assert cache_get(db, "cache-key-1", ttl_days=0) is None
    assert cache_get(db, "cache-key-2", ttl_days=0) is None


def test_cache_purge_requires_login(dash_client):
    r = dash_client.post("/dashboard/cache/purge", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/login"


def test_key_detail_404_for_unknown_key(authed_client):
    r = authed_client.get("/dashboard/keys/does-not-exist")
    assert r.status_code == 404


def test_key_detail_page_renders_stats(authed_client, dash_client):
    authed_client.post("/dashboard/keys", data={"label": "stats-key"})
    key_id = list_api_keys(dash_client.app.state.db)[0]["id"]

    r = authed_client.get(f"/dashboard/keys/{key_id}")
    assert r.status_code == 200
    assert "stats-key" in r.text


def test_extract_page_renders(authed_client):
    r = authed_client.get("/dashboard/extract")
    assert r.status_code == 200
    assert "pretax_basic" in r.text  # mock fixture names listed in mock mode


def test_extract_submit_runs_pipeline_and_logs_history(
    authed_client, dash_client, sample_jpeg_bytes
):
    r = authed_client.post(
        "/dashboard/extract",
        data={"fixture_name": "pretax_basic"},
        files={"file": ("invoice.jpg", sample_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200
    assert "result-summary-card" in r.text

    row = dash_client.app.state.db.execute(
        "SELECT source, api_key_label FROM requests ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["source"] == "dashboard"
    assert row["api_key_label"] == "dashboard-test"


def test_no_page_leaks_raw_provider_key(authed_client, dash_client, sample_jpeg_bytes):
    secret = "sk-super-secret-provider-key"

    authed_client.post("/dashboard/keys", data={"label": "leak-check"})
    key_id = list_api_keys(dash_client.app.state.db)[0]["id"]
    authed_client.post(
        "/dashboard/extract",
        data={"fixture_name": "pretax_basic"},
        files={"file": ("invoice.jpg", sample_jpeg_bytes, "image/jpeg")},
    )

    pages = [
        "/dashboard",
        "/dashboard/logs",
        "/dashboard/keys",
        f"/dashboard/keys/{key_id}",
        "/dashboard/extract",
        "/dashboard/statistics",
    ]
    for path in pages:
        r = authed_client.get(path)
        assert secret not in r.text, f"{path} leaked the raw provider key"
