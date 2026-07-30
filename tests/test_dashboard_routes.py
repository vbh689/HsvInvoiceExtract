import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard_module
from app.db import cache_get, cache_set, insert_request, list_api_keys
from app.llm import LLMCallResult
from app.settings import ModelConfig


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
    paths = [
        "/dashboard/logs",
        "/dashboard/keys",
        "/dashboard/extract",
        "/dashboard/statistics",
        "/dashboard/tenants",
        "/dashboard/api-docs",
    ]
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


def test_api_docs_page_renders(authed_client):
    r = authed_client.get("/dashboard/api-docs")
    assert r.status_code == 200
    assert "X-API-Key" in r.text
    assert "<table>" in r.text


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
        "requests_by_tenant",
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


def test_dashboard_extract_logs_aggregate_retry_usage(
    authed_client, dash_client, sample_jpeg_bytes, monkeypatch
):
    valid = json.loads(
        (Path(__file__).parent / "fixtures" / "mock_responses" / "default.json").read_text()
    )

    def call(raw_json, *, tokens, cost, latency):
        return LLMCallResult(
            raw_json=raw_json,
            model="dashboard-model",
            latency_ms=latency,
            tokens_in=tokens,
            tokens_out=tokens + 1,
            tokens_total=tokens * 3,
            tokens_cached=1,
            cost_usd=cost,
            cost_source="computed",
            usage_raw={"prompt_tokens": tokens, "total_tokens": tokens * 3},
        )

    class RetryClient:
        def __init__(self):
            self.results = [
                call(dict(valid, line_items=[]), tokens=10, cost=0.001, latency=80.0),
                call(valid, tokens=20, cost=0.002, latency=25.0),
            ]

        async def extract(self, *, images, prompt):
            return self.results.pop(0)

    dash_client.app.state.settings.cache_enabled = False
    monkeypatch.setattr(dashboard_module, "build_client", lambda *args, **kwargs: RetryClient())

    r = authed_client.post(
        "/dashboard/extract",
        files={"file": ("invoice.jpg", sample_jpeg_bytes, "image/jpeg")},
    )

    assert r.status_code == 200
    row = dash_client.app.state.db.execute(
        "SELECT * FROM requests ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    response = json.loads(row["response_json"])
    attempt_usage = json.loads(row["usage_json"])["attempts"]
    assert row["attempt_count"] == response["usage"]["attempts"] == 2
    assert row["tokens_in"] == response["usage"]["tokens_in"] == 30
    assert row["tokens_out"] == response["usage"]["tokens_out"] == 32
    assert row["tokens_total"] == response["usage"]["tokens_total"] == 90
    assert row["tokens_cached"] == 2
    assert row["cost_usd"] == response["usage"]["cost_usd"] == 0.003
    assert row["latency_ms"] == response["usage"]["latency_ms"] == 25.0
    assert [attempt["outcome"] for attempt in attempt_usage] == [
        "zero_line_items",
        "success",
    ]


def _set_two_models(dash_client) -> None:
    dash_client.app.state.settings.llm_models = [
        ModelConfig(name="default-model", price_per_1m_input=0.3, price_per_1m_output=2.5),
        ModelConfig(name="second-model", price_per_1m_input=0.1, price_per_1m_output=0.2),
    ]


def test_extract_page_renders_model_dropdown_with_default_selected(authed_client, dash_client):
    _set_two_models(dash_client)
    r = authed_client.get("/dashboard/extract")
    assert r.status_code == 200
    assert "default-model" in r.text
    assert "second-model" in r.text
    assert 'value="default-model" selected' in r.text


def test_extract_submit_non_default_model_flows_through(
    authed_client, dash_client, sample_jpeg_bytes
):
    _set_two_models(dash_client)
    r = authed_client.post(
        "/dashboard/extract",
        data={"fixture_name": "pretax_basic", "model_name": "second-model"},
        files={"file": ("invoice.jpg", sample_jpeg_bytes, "image/jpeg")},
    )
    assert r.status_code == 200

    row = dash_client.app.state.db.execute(
        "SELECT model FROM requests ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["model"] == "second-model"


def test_tenants_page_renders(authed_client):
    r = authed_client.get("/dashboard/tenants")
    assert r.status_code == 200
    assert "Tenants" in r.text


def test_tenant_detail_page_renders_stats(authed_client, dash_client, sample_jpeg_bytes):
    authed_client.post(
        "/dashboard/extract",
        data={"fixture_name": "pretax_basic"},
        files={"file": ("invoice.jpg", sample_jpeg_bytes, "image/jpeg")},
    )

    r = authed_client.get("/dashboard/tenants/1")
    assert r.status_code == 200
    assert "Tenant: 1" in r.text


def test_statistics_export_by_tenant_csv_returns_one_row_per_tenant(
    authed_client, dash_client, sample_jpeg_bytes
):
    authed_client.post(
        "/dashboard/extract",
        data={"fixture_name": "pretax_basic"},
        files={"file": ("invoice.jpg", sample_jpeg_bytes, "image/jpeg")},
    )

    r = authed_client.get("/dashboard/statistics/export_by_tenant.csv?period=all")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]

    lines = r.text.strip().splitlines()
    assert lines[0] == (
        "tenant_code,request_count,avg_confidence,total_cost_usd,avg_latency_ms,cache_hit_rate"
    )
    assert len(lines) == 2
    assert lines[1].startswith("1,")


# ---- Logs / tenant-detail filters ----


def _seed(conn, request_id, *, tenant_code="1", user_name=None, status="usable"):
    """Inserts an audit-log row directly -- the dashboard's own test-upload
    route always writes tenant "1" / no user, so varied rows have to be
    seeded rather than produced.
    """
    insert_request(
        conn,
        {
            "request_id": request_id,
            "created_at": "2026-07-29T00:00:00+00:00",
            "api_key_id": None,
            "api_key_label": "seeded",
            "source": "api",
            "tenant_code": tenant_code,
            "user_name": user_name,
            "filename": f"{request_id}.jpg",
            "content_type": "image/jpeg",
            "file_bytes": 1234,
            "page_count": 1,
            "status": status,
            "confidence": 0.9,
            "price_basis": "pre_tax",
            "line_count": 2,
            "grand_total": 100_000,
            "cache_hit": 0,
            "model": "fake-model",
            "attempt_count": 1,
            "latency_ms": 500.0,
            "tokens_in": 100,
            "tokens_out": 50,
            "tokens_cached": 0,
            "tokens_total": 150,
            "cost_usd": 0.01,
            "cost_source": "computed",
            "usage_json": "{}",
            "response_json": "{}",
            "error": None,
        },
    )


def test_logs_page_shows_tenant_column(authed_client, dash_client):
    _seed(dash_client.app.state.db, "r1", tenant_code="acme")

    r = authed_client.get("/dashboard/logs?period=all")
    assert r.status_code == 200
    assert "<th>Tenant</th>" in r.text
    assert "acme" in r.text


def test_logs_tenant_filter_narrows_rows(authed_client, dash_client):
    db = dash_client.app.state.db
    _seed(db, "r1", tenant_code="acme")
    _seed(db, "r2", tenant_code="beta")

    r = authed_client.get("/dashboard/logs?period=all&tenant=acme")
    assert "r1.jpg" in r.text
    assert "r2.jpg" not in r.text


def test_logs_user_filter_narrows_rows(authed_client, dash_client):
    db = dash_client.app.state.db
    _seed(db, "r1", user_name="alice")
    _seed(db, "r2", user_name="bob")

    r = authed_client.get("/dashboard/logs?period=all&user=alice")
    assert "r1.jpg" in r.text
    assert "r2.jpg" not in r.text


def test_logs_no_user_sentinel_selects_rows_without_a_user(authed_client, dash_client):
    db = dash_client.app.state.db
    _seed(db, "r1", user_name="alice")
    _seed(db, "r2", user_name=None)

    r = authed_client.get("/dashboard/logs?period=all&user=__none__")
    assert "r2.jpg" in r.text
    assert "r1.jpg" not in r.text


def test_logs_filters_combine(authed_client, dash_client):
    db = dash_client.app.state.db
    _seed(db, "match", tenant_code="acme", user_name="alice", status="unusable")
    _seed(db, "wrong-status", tenant_code="acme", user_name="alice", status="usable")
    _seed(db, "wrong-user", tenant_code="acme", user_name="bob", status="unusable")
    _seed(db, "wrong-tenant", tenant_code="beta", user_name="alice", status="unusable")

    r = authed_client.get("/dashboard/logs?period=all&tenant=acme&user=alice&status=unusable")
    assert "match.jpg" in r.text
    for other in ("wrong-status.jpg", "wrong-user.jpg", "wrong-tenant.jpg"):
        assert other not in r.text


def test_logs_status_filter_applies_before_limit(authed_client, dash_client):
    """Regression: the status filter used to run in Python after the SQL
    LIMIT, so a match outside the newest `limit` rows disappeared.
    """
    db = dash_client.app.state.db
    for i in range(3):
        _seed(db, f"new-{i}", status="usable")
    db.execute(
        "UPDATE requests SET created_at = '2026-07-01T00:00:00+00:00' WHERE request_id = 'new-0'"
    )
    db.commit()
    _seed(db, "old-unusable", status="unusable")
    db.execute(
        "UPDATE requests SET created_at = '2026-06-01T00:00:00+00:00' "
        "WHERE request_id = 'old-unusable'"
    )
    db.commit()

    r = authed_client.get("/dashboard/logs?period=all&status=unusable&limit=2")
    assert "old-unusable.jpg" in r.text


def test_logs_period_buttons_preserve_tenant_and_user(authed_client, dash_client):
    _seed(dash_client.app.state.db, "r1", tenant_code="acme", user_name="alice")

    r = authed_client.get("/dashboard/logs?period=all&tenant=acme&user=alice")
    assert '<input type="hidden" name="tenant" value="acme">' in r.text
    assert '<input type="hidden" name="user" value="alice">' in r.text


def test_tenant_detail_user_filter_narrows_table_but_not_tiles(authed_client, dash_client):
    db = dash_client.app.state.db
    _seed(db, "r1", tenant_code="acme", user_name="alice")
    _seed(db, "r2", tenant_code="acme", user_name="bob")
    _seed(db, "r3", tenant_code="acme", user_name=None)

    r = authed_client.get("/dashboard/tenants/acme?period=all&user=alice")
    assert r.status_code == 200
    assert "r1.jpg" not in r.text  # the tenant table has no Filename column
    # Recent-requests table is scoped to alice...
    assert ">alice<" in r.text
    assert ">bob<" not in r.text
    # ...while the Requests tile stays whole-tenant.
    assert '<div class="tile-value">3</div>' in r.text


def test_tenant_detail_user_dropdown_lists_that_tenants_users_only(authed_client, dash_client):
    db = dash_client.app.state.db
    _seed(db, "r1", tenant_code="acme", user_name="alice")
    _seed(db, "r2", tenant_code="beta", user_name="zoe")

    r = authed_client.get("/dashboard/tenants/acme?period=all")
    assert "alice" in r.text
    assert "zoe" not in r.text


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
