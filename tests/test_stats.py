import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.db import create_api_key, get_connection, init_db, insert_request, revoke_api_key
from app.settings import Settings
from app.stats import (
    api_key_summary,
    cache_growth_over_time,
    cache_stats,
    confidence_over_time,
    errors_over_time,
    findings_by_code,
    key_stats,
    latency_percentiles,
    overview_tiles,
    pick_bucket,
    price_basis_breakdown,
    recent_requests,
    requests_by_key,
    requests_by_key_stats,
    requests_by_model,
    requests_by_tenant,
    requests_by_tenant_stats,
    requests_over_time,
    resolve_period,
    statistics_tiles,
    status_breakdown,
    tenant_stats,
)

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
# 2026-07-29 02:30 local (VN, UTC+7) == 2026-07-28 19:30 UTC -- deliberately
# chosen close to local midnight so a UTC-boundary bug would put "today" on
# the wrong calendar day.
NOW = datetime(2026, 7, 29, 2, 30, tzinfo=TZ).astimezone(UTC)


def _ensure_key(conn, key_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO api_keys (id, label, key_hash, key_prefix, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (key_id, key_id, f"hash-{key_id}", key_id[:8], "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path):
    conn = get_connection(Settings(database_path=str(tmp_path / "app.db")))
    init_db(conn)
    _ensure_key(conn, "key-1")
    _ensure_key(conn, "key-a")
    _ensure_key(conn, "key-b")
    yield conn
    conn.close()


def _row(**overrides) -> dict:
    row = {
        "request_id": overrides.pop("request_id", "req-1"),
        "created_at": "2026-07-29T00:00:00+00:00",
        "api_key_id": "key-1",
        "api_key_label": "prod",
        "source": "api",
        "tenant_code": "1",
        "filename": "invoice.jpg",
        "content_type": "image/jpeg",
        "file_bytes": 1234,
        "page_count": 1,
        "status": "usable",
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
    }
    row.update(overrides)
    return row


def _findings_row(request_id: str, created_at: str, findings: list[dict]) -> dict:
    return _row(
        request_id=request_id,
        created_at=created_at,
        response_json=json.dumps({"findings": findings}),
    )


# ---- resolve_period ----


def test_resolve_period_today_uses_local_midnight_not_utc_midnight():
    start, end, meta = resolve_period("today", None, None, NOW)
    # Local midnight 2026-07-29 in VN is 2026-07-28T17:00:00Z, not the UTC
    # midnight (2026-07-29T00:00:00Z) a naive implementation would produce.
    assert start == datetime(2026, 7, 28, 17, 0, tzinfo=UTC)
    assert end == NOW
    assert meta["period"] == "today"


def test_resolve_period_yesterday():
    start, end, meta = resolve_period("yesterday", None, None, NOW)
    assert start == datetime(2026, 7, 27, 17, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 28, 17, 0, tzinfo=UTC)
    assert meta["period"] == "yesterday"


@pytest.mark.parametrize("period,n", [("3d", 3), ("7d", 7), ("14d", 14), ("30d", 30)])
def test_resolve_period_n_day_windows_include_today(period, n):
    from datetime import timedelta

    start, end, _ = resolve_period(period, None, None, NOW)
    local_today_midnight = datetime(2026, 7, 28, 17, 0, tzinfo=UTC)
    assert start == local_today_midnight - timedelta(days=n - 1)
    assert end == NOW


def test_resolve_period_default_is_today():
    _, _, meta = resolve_period(None, None, None, NOW)
    assert meta["period"] == "today"


def test_resolve_period_this_month():
    start, end, meta = resolve_period("this_month", None, None, NOW)
    assert start == datetime(2026, 6, 30, 17, 0, tzinfo=UTC)  # local 2026-07-01 00:00
    assert end == NOW
    assert meta["period"] == "this_month"


def test_resolve_period_last_month():
    start, end, meta = resolve_period("last_month", None, None, NOW)
    assert start == datetime(2026, 5, 31, 17, 0, tzinfo=UTC)  # local 2026-06-01 00:00
    assert end == datetime(2026, 6, 30, 17, 0, tzinfo=UTC)  # local 2026-07-01 00:00
    assert meta["period"] == "last_month"


@pytest.mark.parametrize("period,months", [("3mo", 3), ("6mo", 6)])
def test_resolve_period_n_months(period, months):
    start, end, _ = resolve_period(period, None, None, NOW)
    assert end == NOW
    assert start < datetime(2026, 6, 30, 17, 0, tzinfo=UTC)


def test_resolve_period_all_has_no_bounds():
    start, end, meta = resolve_period("all", None, None, NOW)
    assert start is None
    assert end is None
    assert meta["period"] == "all"


def test_resolve_period_custom_uses_explicit_bounds():
    start, end, meta = resolve_period("custom", "2026-07-01T00:00", "2026-07-15T00:00", NOW)
    assert start == datetime(2026, 6, 30, 17, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 14, 17, 0, tzinfo=UTC)
    assert meta["period"] == "custom"


def test_resolve_period_unknown_key_raises():
    with pytest.raises(ValueError):
        resolve_period("not-a-real-period", None, None, NOW)


# ---- pick_bucket ----


def test_pick_bucket_short_range_is_hourly():
    start = NOW.replace(hour=0)
    assert pick_bucket(start, NOW, NOW) == "hour"


def test_pick_bucket_medium_range_is_daily():
    from datetime import timedelta

    assert pick_bucket(NOW - timedelta(days=30), NOW, NOW) == "day"


def test_pick_bucket_long_range_is_monthly():
    from datetime import timedelta

    assert pick_bucket(NOW - timedelta(days=400), NOW, NOW) == "month"


def test_pick_bucket_unbounded_start_falls_back_to_monthly_window():
    assert pick_bucket(None, NOW, NOW) == "month"


# ---- aggregation ----


def test_overview_tiles_counts_and_status_split(db):
    insert_request(db, _row(request_id="r1", status="usable"))
    insert_request(db, _row(request_id="r2", status="needs_human_review"))
    insert_request(db, _row(request_id="r3", status="unusable"))

    tiles = overview_tiles(db, None, None)
    assert tiles["total_requests"] == 3
    assert tiles["usable_count"] == 1
    assert tiles["needs_human_review_count"] == 1
    assert tiles["unusable_count"] == 1


def test_overview_tiles_cache_hit_rate(db):
    insert_request(db, _row(request_id="r1", cache_hit=0))
    insert_request(db, _row(request_id="r2", cache_hit=1))

    tiles = overview_tiles(db, None, None)
    assert tiles["cache_hit_rate"] == pytest.approx(0.5)


def test_overview_tiles_cost_saved_by_cache_only_counts_cache_hits(db):
    insert_request(db, _row(request_id="r1", cache_hit=0, cost_usd=0.02, cost_source="computed"))
    insert_request(db, _row(request_id="r2", cache_hit=1, cost_usd=0.02, cost_source="cache_hit"))

    tiles = overview_tiles(db, None, None)
    # Billed cost only counts the real (non-cached) call; cost saved counts
    # only the cache hit's avoided cost -- see app.pipeline's avoided_cost_usd.
    assert tiles["total_cost_usd"] == pytest.approx(0.02)
    assert tiles["cost_saved_usd"] == pytest.approx(0.02)


def test_overview_tiles_retry_rate(db):
    insert_request(db, _row(request_id="r1", attempt_count=1))
    insert_request(db, _row(request_id="r2", attempt_count=2))

    tiles = overview_tiles(db, None, None)
    assert tiles["retry_rate"] == pytest.approx(0.5)


def test_overview_tiles_respects_period_window(db):
    insert_request(db, _row(request_id="in", created_at="2026-07-15T00:00:00+00:00"))
    insert_request(db, _row(request_id="out", created_at="2026-01-01T00:00:00+00:00"))

    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 8, 1, tzinfo=UTC)
    tiles = overview_tiles(db, start, end)
    assert tiles["total_requests"] == 1


def test_status_breakdown(db):
    insert_request(db, _row(request_id="r1", status="usable"))
    insert_request(db, _row(request_id="r2", status="usable"))
    insert_request(db, _row(request_id="r3", status="unusable"))

    breakdown = status_breakdown(db, None, None)
    assert breakdown == {"usable": 2, "unusable": 1}


def test_requests_over_time_buckets_by_day(db):
    insert_request(db, _row(request_id="r1", created_at="2026-07-10T05:00:00+00:00"))
    insert_request(db, _row(request_id="r2", created_at="2026-07-10T20:00:00+00:00"))
    insert_request(db, _row(request_id="r3", created_at="2026-07-11T05:00:00+00:00"))

    rows = requests_over_time(db, None, None, "day")
    buckets = {row["bucket"]: row["count"] for row in rows}
    assert buckets["2026-07-10"] == 2
    assert buckets["2026-07-11"] == 1


def test_requests_by_key(db):
    insert_request(db, _row(request_id="r1", api_key_id="key-a"))
    insert_request(db, _row(request_id="r2", api_key_id="key-a"))
    insert_request(db, _row(request_id="r3", api_key_id="key-b"))

    rows = requests_by_key(db, None, None)
    counts = {row["api_key_id"]: row["count"] for row in rows}
    assert counts == {"key-a": 2, "key-b": 1}


def test_key_stats_scopes_to_single_key(db):
    insert_request(db, _row(request_id="r1", api_key_id="key-a", tokens_total=100, cost_usd=0.01))
    insert_request(db, _row(request_id="r2", api_key_id="key-a", tokens_total=200, cost_usd=0.02))
    insert_request(db, _row(request_id="r3", api_key_id="key-b", tokens_total=999, cost_usd=9.0))

    stats = key_stats(db, "key-a", None, None)
    assert stats["total_requests"] == 2
    assert stats["tokens_total"] == 300
    assert stats["total_cost_usd"] == pytest.approx(0.03)


def test_findings_by_code_counts_across_requests(db):
    insert_request(
        db,
        _findings_row(
            "r1",
            "2026-07-10T00:00:00+00:00",
            [
                {"code": "TAX_RATE_INFERRED", "severity": "info", "message": "x"},
                {"code": "SUBTOTAL_INFERRED", "severity": "warning", "message": "x"},
            ],
        ),
    )
    insert_request(
        db,
        _findings_row(
            "r2",
            "2026-07-11T00:00:00+00:00",
            [{"code": "TAX_RATE_INFERRED", "severity": "info", "message": "x"}],
        ),
    )

    results = findings_by_code(db, None, None)
    by_code = {row["code"]: row["count"] for row in results}
    assert by_code["TAX_RATE_INFERRED"] == 2
    assert by_code["SUBTOTAL_INFERRED"] == 1


def test_findings_by_code_ranks_errors_before_warnings_before_info(db):
    insert_request(
        db,
        _findings_row(
            "r1",
            "2026-07-10T00:00:00+00:00",
            [
                {"code": "TAX_RATE_INFERRED", "severity": "info", "message": "x"},
                {"code": "DOCUMENT_TOTALS_UNRECONCILABLE", "severity": "error", "message": "x"},
                {"code": "SUBTOTAL_INFERRED", "severity": "warning", "message": "x"},
            ],
        ),
    )

    results = findings_by_code(db, None, None)
    assert [row["code"] for row in results] == [
        "DOCUMENT_TOTALS_UNRECONCILABLE",
        "SUBTOTAL_INFERRED",
        "TAX_RATE_INFERRED",
    ]


def test_findings_by_code_ignores_requests_with_no_findings(db):
    insert_request(db, _row(request_id="r1", response_json=json.dumps({"findings": []})))

    assert findings_by_code(db, None, None) == []


# ---- statistics page ----


def test_statistics_tiles_error_rate_and_distinct_models(db):
    insert_request(db, _row(request_id="r1", model="model-a", error=None))
    insert_request(db, _row(request_id="r2", model="model-b", error=None))
    insert_request(db, _row(request_id="r3", model="model-a", error="boom"))

    tiles = statistics_tiles(db, None, None)
    assert tiles["total_requests"] == 3
    assert tiles["error_count"] == 1
    assert tiles["error_rate"] == pytest.approx(1 / 3)
    assert tiles["distinct_model_count"] == 2


def test_requests_by_model_groups_and_computes_averages(db):
    insert_request(
        db,
        _row(
            request_id="r1",
            model="model-a",
            confidence=0.8,
            cache_hit=0,
            cost_usd=0.02,
            latency_ms=100.0,
        ),
    )
    insert_request(
        db,
        _row(
            request_id="r2",
            model="model-a",
            confidence=1.0,
            cache_hit=0,
            cost_usd=0.04,
            latency_ms=300.0,
        ),
    )
    insert_request(
        db,
        _row(
            request_id="r3",
            model="model-b",
            confidence=0.5,
            cache_hit=1,
            cost_usd=0.5,
            latency_ms=999.0,
        ),
    )

    rows = requests_by_model(db, None, None)
    by_model = {row["model"]: row for row in rows}
    assert by_model["model-a"]["count"] == 2
    assert by_model["model-a"]["avg_confidence"] == pytest.approx(0.9)
    assert by_model["model-a"]["total_cost_usd"] == pytest.approx(0.06)
    assert by_model["model-a"]["avg_latency_ms"] == pytest.approx(200.0)
    # cache hits are excluded from cost/latency, same convention as overview_tiles
    assert by_model["model-b"]["total_cost_usd"] == pytest.approx(0.0)
    assert by_model["model-b"]["avg_latency_ms"] == pytest.approx(0.0)


def test_requests_by_key_stats_computes_per_key_averages_and_cache_rate(db):
    insert_request(
        db,
        _row(
            request_id="r1",
            api_key_id="key-a",
            confidence=0.8,
            cache_hit=0,
            cost_usd=0.02,
            latency_ms=100.0,
        ),
    )
    insert_request(
        db, _row(request_id="r2", api_key_id="key-a", confidence=1.0, cache_hit=1, cost_usd=0.5)
    )

    rows = requests_by_key_stats(db, None, None)
    row = next(r for r in rows if r["api_key_id"] == "key-a")
    assert row["count"] == 2
    assert row["avg_confidence"] == pytest.approx(0.9)
    assert row["total_cost_usd"] == pytest.approx(0.02)
    assert row["avg_latency_ms"] == pytest.approx(100.0)
    assert row["cache_hit_rate"] == pytest.approx(0.5)


def test_price_basis_breakdown_groups_by_basis(db):
    insert_request(db, _row(request_id="r1", price_basis="pre_tax"))
    insert_request(db, _row(request_id="r2", price_basis="pre_tax"))
    insert_request(db, _row(request_id="r3", price_basis="post_tax"))
    insert_request(db, _row(request_id="r4", price_basis="unknown"))

    breakdown = price_basis_breakdown(db, None, None)
    assert breakdown == {"pre_tax": 2, "post_tax": 1, "unknown": 1}


def test_confidence_over_time_buckets_correctly(db):
    insert_request(
        db,
        _row(request_id="r1", created_at="2026-07-10T05:00:00+00:00", confidence=0.8),
    )
    insert_request(
        db,
        _row(request_id="r2", created_at="2026-07-10T20:00:00+00:00", confidence=1.0),
    )

    rows = confidence_over_time(db, None, None, "day")
    by_bucket = {row["bucket"]: row["avg_confidence"] for row in rows}
    assert by_bucket["2026-07-10"] == pytest.approx(0.9)


def test_errors_over_time_counts_non_null_error_column(db):
    insert_request(db, _row(request_id="r1", created_at="2026-07-10T05:00:00+00:00", error=None))
    insert_request(db, _row(request_id="r2", created_at="2026-07-10T20:00:00+00:00", error="boom"))

    rows = errors_over_time(db, None, None, "day")
    by_bucket = {row["bucket"]: row for row in rows}
    assert by_bucket["2026-07-10"]["total"] == 2
    assert by_bucket["2026-07-10"]["error_count"] == 1


def test_latency_percentiles_excludes_cache_hits_and_nulls(db):
    for i, latency in enumerate([100.0, 200.0, 300.0, 400.0, 500.0]):
        insert_request(db, _row(request_id=f"r{i}", cache_hit=0, latency_ms=latency))
    insert_request(db, _row(request_id="cached", cache_hit=1, latency_ms=9999.0))
    insert_request(db, _row(request_id="no-latency", cache_hit=0, latency_ms=None))

    result = latency_percentiles(db, None, None)
    assert result["p50"] is not None
    assert 100.0 <= result["p50"] <= 500.0
    assert result["p99"] <= 500.0


def test_latency_percentiles_empty_period_returns_none(db):
    result = latency_percentiles(db, None, None)
    assert result == {"p50": None, "p95": None, "p99": None}


def test_cache_growth_over_time_cumulative_total(db):
    db.execute(
        "INSERT INTO extraction_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
        ("k1", "{}", "2026-07-10T05:00:00+00:00"),
    )
    db.execute(
        "INSERT INTO extraction_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
        ("k2", "{}", "2026-07-10T20:00:00+00:00"),
    )
    db.execute(
        "INSERT INTO extraction_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
        ("k3", "{}", "2026-07-11T05:00:00+00:00"),
    )
    db.commit()

    rows = cache_growth_over_time(db, None, None, "day")
    by_bucket = {row["bucket"]: row["cumulative_total"] for row in rows}
    assert by_bucket["2026-07-10"] == 2
    assert by_bucket["2026-07-11"] == 3


def test_cache_stats_totals_and_bounds(db):
    db.execute(
        "INSERT INTO extraction_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
        ("k1", "{}", "2026-07-10T05:00:00+00:00"),
    )
    db.execute(
        "INSERT INTO extraction_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
        ("k2", "{}", "2026-07-11T05:00:00+00:00"),
    )
    db.commit()

    result = cache_stats(db)
    assert result["total_entries"] == 2
    assert result["oldest_entry_at"] == "2026-07-10T05:00:00+00:00"
    assert result["newest_entry_at"] == "2026-07-11T05:00:00+00:00"


def test_cache_stats_empty(db):
    result = cache_stats(db)
    assert result == {"total_entries": 0, "oldest_entry_at": None, "newest_entry_at": None}


# ---- tenants ----


def test_requests_by_tenant(db):
    insert_request(db, _row(request_id="r1", tenant_code="acme"))
    insert_request(db, _row(request_id="r2", tenant_code="acme"))
    insert_request(db, _row(request_id="r3", tenant_code="beta"))

    rows = requests_by_tenant(db, None, None)
    counts = {row["tenant_code"]: row["count"] for row in rows}
    assert counts == {"acme": 2, "beta": 1}


def test_tenant_stats_scopes_to_single_tenant(db):
    insert_request(db, _row(request_id="r1", tenant_code="acme", tokens_total=100, cost_usd=0.01))
    insert_request(db, _row(request_id="r2", tenant_code="acme", tokens_total=200, cost_usd=0.02))
    insert_request(db, _row(request_id="r3", tenant_code="beta", tokens_total=999, cost_usd=9.0))

    stats = tenant_stats(db, "acme", None, None)
    assert stats["total_requests"] == 2
    assert stats["tokens_total"] == 300
    assert stats["total_cost_usd"] == pytest.approx(0.03)


def test_requests_by_tenant_stats_computes_per_tenant_averages_and_cache_rate(db):
    insert_request(
        db,
        _row(
            request_id="r1",
            tenant_code="acme",
            confidence=0.8,
            cache_hit=0,
            cost_usd=0.02,
            latency_ms=100.0,
        ),
    )
    insert_request(
        db, _row(request_id="r2", tenant_code="acme", confidence=1.0, cache_hit=1, cost_usd=0.5)
    )

    rows = requests_by_tenant_stats(db, None, None)
    row = next(r for r in rows if r["tenant_code"] == "acme")
    assert row["count"] == 2
    assert row["avg_confidence"] == pytest.approx(0.9)
    assert row["total_cost_usd"] == pytest.approx(0.02)
    assert row["avg_latency_ms"] == pytest.approx(100.0)
    assert row["cache_hit_rate"] == pytest.approx(0.5)


def test_recent_requests_filters_by_tenant_code_independently_of_api_key(db):
    insert_request(db, _row(request_id="r1", api_key_id="key-a", tenant_code="acme"))
    insert_request(db, _row(request_id="r2", api_key_id="key-a", tenant_code="beta"))
    insert_request(db, _row(request_id="r3", api_key_id="key-b", tenant_code="acme"))

    rows = recent_requests(db, None, None, tenant_code="acme")
    assert {row["request_id"] for row in rows} == {"r1", "r3"}


def test_api_key_summary_active_vs_revoked(db):
    active = create_api_key(db, "active-key")
    to_revoke = create_api_key(db, "revoked-key")
    revoke_api_key(db, to_revoke["id"])

    result = api_key_summary(db)
    # +3 pre-seeded keys from the `db` fixture (`key-1`, `key-a`, `key-b`)
    assert result["total_keys"] == 5
    assert result["active_count"] == 4
    assert result["revoked_count"] == 1
    assert active["id"]  # sanity: created key has an id
