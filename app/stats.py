"""Time-period resolution and SQL aggregation over `requests` for the
dashboard. Aggregation is real SQL against indexed columns (`created_at`,
`api_key_id`), not a Python loop over history -- see `idx_requests_key_time`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_PERIOD = "today"
_N_DAY_PERIODS = {"3d": 3, "7d": 7, "14d": 14, "30d": 30}
_N_MONTH_PERIODS = {"3mo": 3, "6mo": 6}


def _add_months(dt: datetime, months: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) - months
    year, month = divmod(total, 12)
    return dt.replace(year=year, month=month + 1, day=1)


def _local_midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_local(value: str, zone: ZoneInfo) -> datetime:
    """Parses a `datetime-local`-style or ISO string. Naive input is
    interpreted as APP_TIMEZONE local time (what a browser's
    `<input type="datetime-local">` sends); tz-aware input is respected
    as-is.
    """
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=zone)


def resolve_period(
    period: str | None,
    start: str | None,
    end: str | None,
    now: datetime,
    tz: str = "Asia/Ho_Chi_Minh",
) -> tuple[datetime | None, datetime | None, dict]:
    """Resolves a period key (or explicit start/end) into a UTC `[start, end)`
    window plus display metadata. `now` must be tz-aware.

    Day/month boundaries ("today", "this_month", ...) are computed in `tz`,
    not UTC -- midnight in Asia/Ho_Chi_Minh is 17:00 UTC the day before, and
    resolving boundaries in UTC would make "today" start seven hours into
    the local day.
    """
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)

    # The filter form has one <input type="datetime-local"> pair shared by
    # both the quick-select buttons and the "Apply custom range" button, so
    # clicking a quick-select button (period=<key>) still submits whatever
    # start/end values are currently in those fields (left over from the
    # previously resolved period). Only treat start/end as authoritative
    # when the user actually asked for a custom range -- explicitly via
    # period=custom, or implicitly by supplying start/end without a period
    # at all (e.g. a hand-built URL / API call).
    if period == "custom" or (period is None and (start or end)):
        start_dt = _parse_local(start, zone).astimezone(UTC) if start else None
        end_dt = _parse_local(end, zone).astimezone(UTC) if end else None
        meta = {"period": "custom", "start": start or "", "end": end or ""}
        return start_dt, end_dt, meta

    key = period or DEFAULT_PERIOD
    today_start = _local_midnight(local_now)

    if key == "today":
        start_local, end_local = today_start, local_now
    elif key == "yesterday":
        start_local = today_start - timedelta(days=1)
        end_local = today_start
    elif key in _N_DAY_PERIODS:
        n = _N_DAY_PERIODS[key]
        start_local = today_start - timedelta(days=n - 1)
        end_local = local_now
    elif key == "this_month":
        start_local = today_start.replace(day=1)
        end_local = local_now
    elif key == "last_month":
        first_of_this_month = today_start.replace(day=1)
        first_of_last_month = _add_months(first_of_this_month, 1)
        start_local = first_of_last_month
        end_local = first_of_this_month
    elif key in _N_MONTH_PERIODS:
        n = _N_MONTH_PERIODS[key]
        start_local = _add_months(today_start.replace(day=1), n)
        end_local = local_now
    elif key == "all":
        start_local, end_local = None, None
    else:
        raise ValueError(f"unknown period: {key!r}")

    start_utc = start_local.astimezone(UTC) if start_local else None
    end_utc = end_local.astimezone(UTC) if end_local else None

    meta = {
        "period": key,
        "start": start_local.strftime("%Y-%m-%dT%H:%M") if start_local else "",
        "end": end_local.strftime("%Y-%m-%dT%H:%M") if end_local else "",
    }
    return start_utc, end_utc, meta


def pick_bucket(start_utc: datetime | None, end_utc: datetime | None, now: datetime) -> str:
    """Chart time-bucket width: hourly for short ranges, daily for medium
    ones, monthly for long/unbounded ones. Bucketing itself runs in UTC via
    SQLite `strftime` -- only period *boundaries* need APP_TIMEZONE.
    """
    span_start = start_utc or (now - timedelta(days=365))
    span_end = end_utc or now
    span = span_end - span_start
    if span <= timedelta(days=2):
        return "hour"
    if span <= timedelta(days=120):
        return "day"
    return "month"


_BUCKET_FORMAT = {
    "hour": "%Y-%m-%dT%H:00:00",
    "day": "%Y-%m-%d",
    "month": "%Y-%m",
}


def _where_clause(start_utc: datetime | None, end_utc: datetime | None) -> tuple[str, list]:
    clauses = []
    params: list = []
    if start_utc is not None:
        clauses.append("created_at >= ?")
        params.append(start_utc.isoformat())
    if end_utc is not None:
        clauses.append("created_at < ?")
        params.append(end_utc.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def overview_tiles(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> dict:
    where, params = _where_clause(start_utc, end_utc)
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_requests,
            SUM(CASE WHEN status = 'usable' THEN 1 ELSE 0 END) AS usable_count,
            SUM(CASE WHEN status = 'needs_human_review' THEN 1 ELSE 0 END) AS review_count,
            SUM(CASE WHEN status = 'unusable' THEN 1 ELSE 0 END) AS unusable_count,
            AVG(confidence) AS avg_confidence,
            SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hits,
            SUM(CASE WHEN cache_hit = 0 THEN tokens_total ELSE 0 END) AS billed_tokens_total,
            SUM(CASE WHEN cache_hit = 0 THEN cost_usd ELSE 0 END) AS total_cost_usd,
            SUM(CASE WHEN cache_hit = 1 THEN cost_usd ELSE 0 END) AS cost_saved_usd,
            SUM(CASE WHEN cache_hit = 0 THEN latency_ms ELSE 0 END) AS billed_latency_sum,
            SUM(CASE WHEN cache_hit = 0 AND cost_source = 'computed' THEN 1 ELSE 0 END)
                AS computed_cost_rows,
            SUM(CASE WHEN attempt_count > 1 THEN 1 ELSE 0 END) AS retried_count
        FROM requests
        {where}
        """,
        params,
    ).fetchone()

    total = row["total_requests"] or 0
    billed = total - (row["cache_hits"] or 0)
    return {
        "total_requests": total,
        "usable_count": row["usable_count"] or 0,
        "needs_human_review_count": row["review_count"] or 0,
        "unusable_count": row["unusable_count"] or 0,
        "avg_confidence": row["avg_confidence"] or 0.0,
        "cache_hit_rate": (row["cache_hits"] or 0) / total if total else 0.0,
        "tokens_total": row["billed_tokens_total"] or 0,
        "total_cost_usd": row["total_cost_usd"] or 0.0,
        "cost_saved_usd": row["cost_saved_usd"] or 0.0,
        "avg_latency_ms": (row["billed_latency_sum"] or 0.0) / billed if billed else 0.0,
        "retry_rate": (row["retried_count"] or 0) / total if total else 0.0,
        "has_estimated_cost": bool(row["computed_cost_rows"]),
    }


def status_breakdown(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> dict[str, int]:
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS count FROM requests {where} GROUP BY status", params
    ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def requests_over_time(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None, bucket: str
) -> list[dict]:
    fmt = _BUCKET_FORMAT[bucket]
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', created_at) AS bucket, status, COUNT(*) AS count
        FROM requests
        {where}
        GROUP BY bucket, status
        ORDER BY bucket
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def tokens_over_time(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None, bucket: str
) -> list[dict]:
    fmt = _BUCKET_FORMAT[bucket]
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', created_at) AS bucket,
               SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out
        FROM requests
        {where}
        GROUP BY bucket
        ORDER BY bucket
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def cost_over_time(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None, bucket: str
) -> list[dict]:
    fmt = _BUCKET_FORMAT[bucket]
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', created_at) AS bucket,
               SUM(CASE WHEN cache_hit = 0 THEN cost_usd ELSE 0 END) AS cost_usd
        FROM requests
        {where}
        GROUP BY bucket
        ORDER BY bucket
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def findings_by_code(
    conn: sqlite3.Connection,
    start_utc: datetime | None,
    end_utc: datetime | None,
    *,
    limit: int = 10,
) -> list[dict]:
    """Findings live inside `requests.response_json`, not a normalized
    table -- there's no separate findings table to keep three tables simple.
    SQLite's JSON1 extension (bundled since 3.38 / Python 3.11+) walks the
    array directly rather than deserializing every row in Python.
    """
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT f.value ->> 'code' AS code, f.value ->> 'severity' AS severity, COUNT(*) AS count
        FROM requests, json_each(requests.response_json, '$.findings') AS f
        {where}
        GROUP BY code, severity
        ORDER BY count DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    results = [dict(row) for row in rows]
    results.sort(key=lambda r: (_SEVERITY_RANK.get(r["severity"], 99), -r["count"]))
    return results


def requests_by_key(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> list[dict]:
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT api_key_id, api_key_label, COUNT(*) AS count
        FROM requests
        {where}
        GROUP BY api_key_id
        ORDER BY count DESC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def key_stats(
    conn: sqlite3.Connection,
    key_id: str,
    start_utc: datetime | None,
    end_utc: datetime | None,
) -> dict:
    where, params = _where_clause(start_utc, end_utc)
    key_clause = "api_key_id = ?"
    where = f"{where} AND {key_clause}" if where else f"WHERE {key_clause}"
    params = [*params, key_id]

    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_requests,
            SUM(CASE WHEN status = 'usable' THEN 1 ELSE 0 END) AS usable_count,
            SUM(CASE WHEN status = 'needs_human_review' THEN 1 ELSE 0 END) AS review_count,
            SUM(CASE WHEN status = 'unusable' THEN 1 ELSE 0 END) AS unusable_count,
            AVG(confidence) AS avg_confidence,
            SUM(CASE WHEN cache_hit = 0 THEN tokens_in ELSE 0 END) AS tokens_in,
            SUM(CASE WHEN cache_hit = 0 THEN tokens_out ELSE 0 END) AS tokens_out,
            SUM(CASE WHEN cache_hit = 0 THEN tokens_total ELSE 0 END) AS tokens_total,
            SUM(CASE WHEN cache_hit = 0 THEN cost_usd ELSE 0 END) AS total_cost_usd,
            SUM(CASE WHEN cache_hit = 0 THEN latency_ms ELSE 0 END) AS billed_latency_sum,
            SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hits,
            MIN(created_at) AS first_used_at,
            MAX(created_at) AS last_used_at
        FROM requests
        {where}
        """,
        params,
    ).fetchone()

    total = row["total_requests"] or 0
    billed = total - (row["cache_hits"] or 0)
    return {
        "total_requests": total,
        "status_counts": {
            "usable": row["usable_count"] or 0,
            "needs_human_review": row["review_count"] or 0,
            "unusable": row["unusable_count"] or 0,
        },
        "avg_confidence": row["avg_confidence"] or 0.0,
        "tokens_in": row["tokens_in"] or 0,
        "tokens_out": row["tokens_out"] or 0,
        "tokens_total": row["tokens_total"] or 0,
        "total_cost_usd": row["total_cost_usd"] or 0.0,
        "avg_latency_ms": (row["billed_latency_sum"] or 0.0) / billed if billed else 0.0,
        "cache_hit_rate": (row["cache_hits"] or 0) / total if total else 0.0,
        "first_used_at": row["first_used_at"],
        "last_used_at": row["last_used_at"],
    }


def recent_requests(
    conn: sqlite3.Connection,
    start_utc: datetime | None,
    end_utc: datetime | None,
    *,
    api_key_id: str | None = None,
    tenant_code: str | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    where, params = _where_clause(start_utc, end_utc)
    if api_key_id is not None:
        where = f"{where} AND api_key_id = ?" if where else "WHERE api_key_id = ?"
        params = [*params, api_key_id]
    if tenant_code is not None:
        where = f"{where} AND tenant_code = ?" if where else "WHERE tenant_code = ?"
        params = [*params, tenant_code]
    return conn.execute(
        f"SELECT * FROM requests {where} ORDER BY created_at DESC LIMIT ?",
        [*params, limit],
    ).fetchall()


# ---- Tenants ----
# Mirrors of the api_key_id-based functions above, but grouped/filtered by
# tenant_code -- a separate dimension from API key (see plan doc), applies
# whether or not API-key auth is enabled.


def requests_by_tenant(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> list[dict]:
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT tenant_code, COUNT(*) AS count
        FROM requests
        {where}
        GROUP BY tenant_code
        ORDER BY count DESC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def tenant_stats(
    conn: sqlite3.Connection,
    tenant_code: str,
    start_utc: datetime | None,
    end_utc: datetime | None,
) -> dict:
    where, params = _where_clause(start_utc, end_utc)
    tenant_clause = "tenant_code = ?"
    where = f"{where} AND {tenant_clause}" if where else f"WHERE {tenant_clause}"
    params = [*params, tenant_code]

    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_requests,
            SUM(CASE WHEN status = 'usable' THEN 1 ELSE 0 END) AS usable_count,
            SUM(CASE WHEN status = 'needs_human_review' THEN 1 ELSE 0 END) AS review_count,
            SUM(CASE WHEN status = 'unusable' THEN 1 ELSE 0 END) AS unusable_count,
            AVG(confidence) AS avg_confidence,
            SUM(CASE WHEN cache_hit = 0 THEN tokens_in ELSE 0 END) AS tokens_in,
            SUM(CASE WHEN cache_hit = 0 THEN tokens_out ELSE 0 END) AS tokens_out,
            SUM(CASE WHEN cache_hit = 0 THEN tokens_total ELSE 0 END) AS tokens_total,
            SUM(CASE WHEN cache_hit = 0 THEN cost_usd ELSE 0 END) AS total_cost_usd,
            SUM(CASE WHEN cache_hit = 0 THEN latency_ms ELSE 0 END) AS billed_latency_sum,
            SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hits,
            MIN(created_at) AS first_used_at,
            MAX(created_at) AS last_used_at
        FROM requests
        {where}
        """,
        params,
    ).fetchone()

    total = row["total_requests"] or 0
    billed = total - (row["cache_hits"] or 0)
    return {
        "total_requests": total,
        "status_counts": {
            "usable": row["usable_count"] or 0,
            "needs_human_review": row["review_count"] or 0,
            "unusable": row["unusable_count"] or 0,
        },
        "avg_confidence": row["avg_confidence"] or 0.0,
        "tokens_in": row["tokens_in"] or 0,
        "tokens_out": row["tokens_out"] or 0,
        "tokens_total": row["tokens_total"] or 0,
        "total_cost_usd": row["total_cost_usd"] or 0.0,
        "avg_latency_ms": (row["billed_latency_sum"] or 0.0) / billed if billed else 0.0,
        "cache_hit_rate": (row["cache_hits"] or 0) / total if total else 0.0,
        "first_used_at": row["first_used_at"],
        "last_used_at": row["last_used_at"],
    }


def requests_by_tenant_stats(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> list[dict]:
    """Fuller per-tenant breakdown than `requests_by_tenant` (count-only) --
    adds avg confidence, cost, latency, and cache-hit rate for the
    Statistics page's per-tenant table and CSV export.
    """
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT
            tenant_code,
            COUNT(*) AS count,
            AVG(confidence) AS avg_confidence,
            SUM(CASE WHEN cache_hit = 0 THEN cost_usd ELSE 0 END) AS total_cost_usd,
            AVG(CASE WHEN cache_hit = 0 THEN latency_ms END) AS avg_latency_ms,
            SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hits
        FROM requests
        {where}
        GROUP BY tenant_code
        ORDER BY count DESC
        """,
        params,
    ).fetchall()
    results = []
    for row in rows:
        count = row["count"] or 0
        results.append(
            {
                "tenant_code": row["tenant_code"],
                "count": count,
                "avg_confidence": row["avg_confidence"] or 0.0,
                "total_cost_usd": row["total_cost_usd"] or 0.0,
                "avg_latency_ms": row["avg_latency_ms"] or 0.0,
                "cache_hit_rate": (row["cache_hits"] or 0) / count if count else 0.0,
            }
        )
    return results


# ---- Statistics page ----


def statistics_tiles(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> dict:
    where, params = _where_clause(start_utc, end_utc)
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_requests,
            SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS error_count,
            COUNT(DISTINCT model) AS distinct_model_count
        FROM requests
        {where}
        """,
        params,
    ).fetchone()
    total = row["total_requests"] or 0
    error_count = row["error_count"] or 0
    return {
        "total_requests": total,
        "error_count": error_count,
        "error_rate": error_count / total if total else 0.0,
        "distinct_model_count": row["distinct_model_count"] or 0,
    }


def requests_by_model(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> list[dict]:
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT
            model,
            COUNT(*) AS count,
            AVG(confidence) AS avg_confidence,
            SUM(CASE WHEN cache_hit = 0 THEN cost_usd ELSE 0 END) AS total_cost_usd,
            AVG(CASE WHEN cache_hit = 0 THEN latency_ms END) AS avg_latency_ms
        FROM requests
        {where}
        GROUP BY model
        ORDER BY count DESC
        """,
        params,
    ).fetchall()
    return [
        {
            "model": row["model"],
            "count": row["count"],
            "avg_confidence": row["avg_confidence"] or 0.0,
            "total_cost_usd": row["total_cost_usd"] or 0.0,
            "avg_latency_ms": row["avg_latency_ms"] or 0.0,
        }
        for row in rows
    ]


def requests_by_key_stats(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> list[dict]:
    """Fuller per-key breakdown than `requests_by_key` (count-only) -- adds
    avg confidence, cost, latency, and cache-hit rate for the Statistics
    page's per-customer table and CSV export.
    """
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT
            api_key_id,
            api_key_label,
            COUNT(*) AS count,
            AVG(confidence) AS avg_confidence,
            SUM(CASE WHEN cache_hit = 0 THEN cost_usd ELSE 0 END) AS total_cost_usd,
            AVG(CASE WHEN cache_hit = 0 THEN latency_ms END) AS avg_latency_ms,
            SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hits
        FROM requests
        {where}
        GROUP BY api_key_id
        ORDER BY count DESC
        """,
        params,
    ).fetchall()
    results = []
    for row in rows:
        count = row["count"] or 0
        results.append(
            {
                "api_key_id": row["api_key_id"],
                "api_key_label": row["api_key_label"],
                "count": count,
                "avg_confidence": row["avg_confidence"] or 0.0,
                "total_cost_usd": row["total_cost_usd"] or 0.0,
                "avg_latency_ms": row["avg_latency_ms"] or 0.0,
                "cache_hit_rate": (row["cache_hits"] or 0) / count if count else 0.0,
            }
        )
    return results


def price_basis_breakdown(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> dict[str, int]:
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"SELECT price_basis, COUNT(*) AS count FROM requests {where} GROUP BY price_basis",
        params,
    ).fetchall()
    return {(row["price_basis"] or "unknown"): row["count"] for row in rows}


def confidence_over_time(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None, bucket: str
) -> list[dict]:
    fmt = _BUCKET_FORMAT[bucket]
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', created_at) AS bucket, AVG(confidence) AS avg_confidence
        FROM requests
        {where}
        GROUP BY bucket
        ORDER BY bucket
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def errors_over_time(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None, bucket: str
) -> list[dict]:
    fmt = _BUCKET_FORMAT[bucket]
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', created_at) AS bucket,
               COUNT(*) AS total,
               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS error_count
        FROM requests
        {where}
        GROUP BY bucket
        ORDER BY bucket
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def latency_percentiles(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None
) -> dict:
    """Nearest-rank percentiles via LIMIT/OFFSET rather than NTILE -- NTILE(100)
    puts each row in its own bucket once there are fewer than 100 rows, so at
    this service's traffic volume it would never hit exactly pct 50/95/99.
    """
    where, params = _where_clause(start_utc, end_utc)
    clause = "cache_hit = 0 AND latency_ms IS NOT NULL"
    where = f"{where} AND {clause}" if where else f"WHERE {clause}"

    total = conn.execute(f"SELECT COUNT(*) AS n FROM requests {where}", params).fetchone()["n"]
    if not total:
        return {"p50": None, "p95": None, "p99": None}

    def _percentile(pct: float) -> float:
        offset = min(total - 1, int(pct * total))
        row = conn.execute(
            f"SELECT latency_ms FROM requests {where} ORDER BY latency_ms LIMIT 1 OFFSET ?",
            [*params, offset],
        ).fetchone()
        return row["latency_ms"]

    return {"p50": _percentile(0.50), "p95": _percentile(0.95), "p99": _percentile(0.99)}


def cache_growth_over_time(
    conn: sqlite3.Connection, start_utc: datetime | None, end_utc: datetime | None, bucket: str
) -> list[dict]:
    fmt = _BUCKET_FORMAT[bucket]
    where, params = _where_clause(start_utc, end_utc)
    rows = conn.execute(
        f"""
        WITH per_bucket AS (
            SELECT strftime('{fmt}', created_at) AS bucket, COUNT(*) AS added
            FROM extraction_cache
            {where}
            GROUP BY bucket
        )
        SELECT bucket, added, SUM(added) OVER (ORDER BY bucket) AS cumulative_total
        FROM per_bucket
        ORDER BY bucket
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def cache_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total_entries, MIN(created_at) AS oldest_entry_at, "
        "MAX(created_at) AS newest_entry_at FROM extraction_cache"
    ).fetchone()
    return {
        "total_entries": row["total_entries"] or 0,
        "oldest_entry_at": row["oldest_entry_at"],
        "newest_entry_at": row["newest_entry_at"],
    }


def api_key_summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total_keys, "
        "SUM(CASE WHEN revoked_at IS NULL THEN 1 ELSE 0 END) AS active_count, "
        "SUM(CASE WHEN revoked_at IS NOT NULL THEN 1 ELSE 0 END) AS revoked_count "
        "FROM api_keys"
    ).fetchone()
    return {
        "total_keys": row["total_keys"] or 0,
        "active_count": row["active_count"] or 0,
        "revoked_count": row["revoked_count"] or 0,
    }
