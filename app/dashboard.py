"""All /dashboard routes: session-cookie login for human operators (unrelated
to the X-API-Key system in app.api), overview with charts, filterable request
log, API key management, per-key stats, and an operator test-upload page.
Server-rendered Jinja2 -- no JS build step, no CDN; charts fetch series from
the JSON endpoint at the bottom of this file.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.db import (
    cache_purge,
    create_api_key,
    get_api_key,
    get_request,
    insert_request,
    list_api_keys,
    revoke_api_key,
)
from app.llm import build_client
from app.normalize.image import SUPPORTED_CONTENT_TYPES
from app.pipeline import extract as run_pipeline
from app.settings import Settings
from app.stats import (
    api_key_summary,
    cache_growth_over_time,
    cache_stats,
    confidence_over_time,
    cost_over_time,
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
    tokens_over_time,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

VALID_STATUSES = {"usable", "needs_human_review", "unusable"}


class NotAuthenticated(Exception):
    """Raised by require_dashboard_login; app.main registers a handler that
    turns this into a redirect to the login page rather than a raw 401,
    since these are browser-facing pages, not an API.
    """


def require_dashboard_login(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise NotAuthenticated()


public_router = APIRouter(prefix="/dashboard")
router = APIRouter(prefix="/dashboard", dependencies=[Depends(require_dashboard_login)])


def _period_args(request: Request) -> tuple[str | None, str | None, str | None]:
    q = request.query_params
    return q.get("period"), q.get("start"), q.get("end")


def _resolve(request: Request) -> tuple[datetime | None, datetime | None, dict]:
    settings: Settings = request.app.state.settings
    period, start, end = _period_args(request)
    return resolve_period(period, start, end, datetime.now(UTC), tz=settings.app_timezone)


# ---- Login / logout (no auth dependency) ----


@public_router.get("/login")
async def login_form(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@public_router.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    settings: Settings = request.app.state.settings
    expected = settings.dashboard_password
    if expected and secrets.compare_digest(password, expected):
        request.session["authenticated"] = True
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid password"}, status_code=401
    )


@public_router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/dashboard/login", status_code=303)


# ---- Overview ----


@router.get("")
async def overview(request: Request):
    db: sqlite3.Connection = request.app.state.db
    start, end, meta = _resolve(request)
    tiles = overview_tiles(db, start, end)
    recent = recent_requests(db, start, end, limit=10)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "tiles": tiles,
            "recent": recent,
            "filter_meta": meta,
            "mock_mode": request.app.state.settings.mock_mode,
        },
    )


@router.get("/api/chart-data")
async def chart_data(request: Request):
    db: sqlite3.Connection = request.app.state.db
    start, end, meta = _resolve(request)
    bucket = pick_bucket(start, end, datetime.now(UTC))
    return JSONResponse(
        {
            "bucket": bucket,
            "period": meta,
            "requests_over_time": requests_over_time(db, start, end, bucket),
            "tokens_over_time": tokens_over_time(db, start, end, bucket),
            "cost_over_time": cost_over_time(db, start, end, bucket),
            "status_breakdown": status_breakdown(db, start, end),
            "findings_by_code": findings_by_code(db, start, end),
            "requests_by_key": requests_by_key(db, start, end),
            "requests_by_tenant": requests_by_tenant(db, start, end),
        }
    )


# ---- Statistics ----


@router.get("/statistics")
async def statistics(request: Request):
    db: sqlite3.Connection = request.app.state.db
    start, end, meta = _resolve(request)
    return templates.TemplateResponse(
        request,
        "statistics.html",
        {
            "tiles": statistics_tiles(db, start, end),
            "latency": latency_percentiles(db, start, end),
            "cache": cache_stats(db),
            "keys_summary": api_key_summary(db),
            "by_model": requests_by_model(db, start, end),
            "by_key": requests_by_key_stats(db, start, end),
            "by_tenant": requests_by_tenant_stats(db, start, end),
            "filter_meta": meta,
            "mock_mode": request.app.state.settings.mock_mode,
        },
    )


@router.get("/api/statistics-chart-data")
async def statistics_chart_data(request: Request):
    db: sqlite3.Connection = request.app.state.db
    start, end, meta = _resolve(request)
    bucket = pick_bucket(start, end, datetime.now(UTC))
    return JSONResponse(
        {
            "bucket": bucket,
            "period": meta,
            "requests_by_model": requests_by_model(db, start, end),
            "price_basis_breakdown": price_basis_breakdown(db, start, end),
            "confidence_over_time": confidence_over_time(db, start, end, bucket),
            "errors_over_time": errors_over_time(db, start, end, bucket),
            "cache_growth_over_time": cache_growth_over_time(db, start, end, bucket),
        }
    )


@router.get("/statistics/export.csv")
async def statistics_export_csv(request: Request):
    db: sqlite3.Connection = request.app.state.db
    start, end, meta = _resolve(request)
    rows = requests_by_key_stats(db, start, end)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "api_key_label",
            "api_key_id",
            "request_count",
            "avg_confidence",
            "total_cost_usd",
            "avg_latency_ms",
            "cache_hit_rate",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["api_key_label"] or "unknown",
                r["api_key_id"] or "",
                r["count"],
                r["avg_confidence"],
                r["total_cost_usd"],
                r["avg_latency_ms"],
                r["cache_hit_rate"],
            ]
        )

    filename = f"statistics-by-key-{meta['period']}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/statistics/export_by_tenant.csv")
async def statistics_export_by_tenant_csv(request: Request):
    db: sqlite3.Connection = request.app.state.db
    start, end, meta = _resolve(request)
    rows = requests_by_tenant_stats(db, start, end)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "tenant_code",
            "request_count",
            "avg_confidence",
            "total_cost_usd",
            "avg_latency_ms",
            "cache_hit_rate",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["tenant_code"],
                r["count"],
                r["avg_confidence"],
                r["total_cost_usd"],
                r["avg_latency_ms"],
                r["cache_hit_rate"],
            ]
        )

    filename = f"statistics-by-tenant-{meta['period']}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/cache/purge")
async def cache_purge_submit(request: Request):
    db: sqlite3.Connection = request.app.state.db
    cache_purge(db)
    return RedirectResponse(url="/dashboard/statistics", status_code=303)


# ---- Logs ----


@router.get("/logs")
async def logs(request: Request, status: str | None = None, limit: int = 50):
    db: sqlite3.Connection = request.app.state.db
    start, end, meta = _resolve(request)
    parsed_status = status.strip() if status and status.strip() in VALID_STATUSES else None

    rows = recent_requests(db, start, end, limit=limit)
    if parsed_status:
        rows = [row for row in rows if row["status"] == parsed_status]

    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "entries": rows,
            "status": parsed_status or "",
            "limit": limit,
            "filter_meta": meta,
            "mock_mode": request.app.state.settings.mock_mode,
        },
    )


@router.get("/logs/{request_id}")
async def log_detail(request: Request, request_id: str):
    db: sqlite3.Connection = request.app.state.db
    row = get_request(db, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    response_json = json.loads(row["response_json"])
    return templates.TemplateResponse(
        request,
        "log_detail.html",
        {
            "entry": row,
            "response": response_json,
            "raw_json": json.dumps(response_json, indent=2, ensure_ascii=False),
            "mock_mode": request.app.state.settings.mock_mode,
        },
    )


# ---- API keys ----


@router.get("/keys")
async def keys_page(request: Request):
    db: sqlite3.Connection = request.app.state.db
    records = list_api_keys(db)
    start, end, _ = resolve_period("all", None, None, datetime.now(UTC))
    counts = {row["api_key_id"]: row["count"] for row in requests_by_key(db, start, end)}
    flashed_key = request.session.pop("flashed_plaintext_key", None)
    return templates.TemplateResponse(
        request,
        "keys.html",
        {
            "records": records,
            "request_counts": counts,
            "flashed_key": flashed_key,
            "mock_mode": request.app.state.settings.mock_mode,
        },
    )


@router.post("/keys")
async def keys_create(request: Request, label: str = Form(...)):
    db: sqlite3.Connection = request.app.state.db
    key = create_api_key(db, label)
    request.session["flashed_plaintext_key"] = key["plaintext"]
    return RedirectResponse(url="/dashboard/keys", status_code=303)


@router.post("/keys/{key_id}/revoke")
async def keys_revoke(request: Request, key_id: str):
    db: sqlite3.Connection = request.app.state.db
    revoke_api_key(db, key_id)
    return RedirectResponse(url="/dashboard/keys", status_code=303)


@router.get("/keys/{key_id}")
async def key_detail(request: Request, key_id: str):
    db: sqlite3.Connection = request.app.state.db
    key = get_api_key(db, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="key not found")

    start, end, meta = _resolve(request)
    stats = key_stats(db, key_id, start, end)
    recent = recent_requests(db, start, end, api_key_id=key_id, limit=20)
    return templates.TemplateResponse(
        request,
        "key_detail.html",
        {
            "key": key,
            "stats": stats,
            "recent": recent,
            "filter_meta": meta,
            "mock_mode": request.app.state.settings.mock_mode,
        },
    )


# ---- Tenants ----


@router.get("/tenants")
async def tenants_page(request: Request):
    db: sqlite3.Connection = request.app.state.db
    start, end, _ = resolve_period("all", None, None, datetime.now(UTC))
    records = requests_by_tenant(db, start, end)
    return templates.TemplateResponse(
        request,
        "tenants.html",
        {
            "records": records,
            "mock_mode": request.app.state.settings.mock_mode,
        },
    )


@router.get("/tenants/{tenant_code}")
async def tenant_detail(request: Request, tenant_code: str):
    db: sqlite3.Connection = request.app.state.db
    start, end, meta = _resolve(request)
    stats = tenant_stats(db, tenant_code, start, end)
    recent = recent_requests(db, start, end, tenant_code=tenant_code, limit=20)
    return templates.TemplateResponse(
        request,
        "tenant_detail.html",
        {
            "tenant_code": tenant_code,
            "stats": stats,
            "recent": recent,
            "filter_meta": meta,
            "mock_mode": request.app.state.settings.mock_mode,
        },
    )


# ---- Operator test-upload page ----


def _extract_ctx(**overrides) -> dict:
    ctx = {
        "result": None,
        "error": None,
        "raw_json": None,
        "fixture_names": [],
        "uploaded_file_name": None,
        "uploaded_data_url": None,
    }
    ctx.update(overrides)
    return ctx


def _fixture_names(settings: Settings) -> list[str]:
    if not settings.mock_mode:
        return []
    from app.llm import DEFAULT_FIXTURE_DIR

    return sorted(p.stem for p in DEFAULT_FIXTURE_DIR.glob("*.json"))


@router.get("/extract")
async def extract_form(request: Request):
    settings: Settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "extract.html",
        _extract_ctx(fixture_names=_fixture_names(settings), mock_mode=settings.mock_mode),
    )


@router.post("/extract")
async def extract_submit(
    request: Request,
    file: UploadFile = File(...),
    fixture_name: str = Form(default="default"),
):
    settings: Settings = request.app.state.settings
    db: sqlite3.Connection = request.app.state.db
    fixture_names = _fixture_names(settings)

    if file.content_type not in SUPPORTED_CONTENT_TYPES:
        return templates.TemplateResponse(
            request,
            "extract.html",
            _extract_ctx(
                error=f"Unsupported content type: {file.content_type}",
                fixture_names=fixture_names,
                mock_mode=settings.mock_mode,
            ),
            status_code=415,
        )

    raw_bytes = await file.read()
    if not raw_bytes:
        return templates.TemplateResponse(
            request,
            "extract.html",
            _extract_ctx(
                error="Empty file", fixture_names=fixture_names, mock_mode=settings.mock_mode
            ),
            status_code=400,
        )
    if len(raw_bytes) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes // (1024 * 1024)
        return templates.TemplateResponse(
            request,
            "extract.html",
            _extract_ctx(
                error=f"File exceeds maximum upload size ({limit_mb} MB)",
                fixture_names=fixture_names,
                mock_mode=settings.mock_mode,
            ),
            status_code=413,
        )

    chosen_fixture = fixture_name if (settings.mock_mode and fixture_name) else "default"
    client = build_client(settings, fixture_name=chosen_fixture)

    result = await run_pipeline(
        raw_bytes=raw_bytes,
        content_type=file.content_type,
        settings=settings,
        client=client,
        db=db,
    )
    response = result.response

    insert_request(
        db,
        {
            "request_id": response.request_id,
            "created_at": response.created_at.isoformat(),
            "api_key_id": None,
            "api_key_label": "dashboard-test",
            "source": "dashboard",
            "tenant_code": "1",
            "filename": file.filename,
            "content_type": file.content_type,
            "file_bytes": len(raw_bytes),
            "page_count": result.page_count,
            "status": response.status,
            "confidence": response.confidence,
            "price_basis": response.price_basis.basis,
            "line_count": len(response.line_items),
            "grand_total": response.document.grand_total_calculated
            if response.document.grand_total_calculated is not None
            else response.document.grand_total_printed,
            "cache_hit": int(result.cache_hit),
            "model": response.usage.model,
            "attempt_count": response.usage.attempts,
            "latency_ms": response.usage.latency_ms,
            "tokens_in": result.avoided_tokens_in if result.cache_hit else response.usage.tokens_in,
            "tokens_out": result.avoided_tokens_out
            if result.cache_hit
            else response.usage.tokens_out,
            "tokens_cached": result.tokens_cached,
            "tokens_total": result.avoided_tokens_total
            if result.cache_hit
            else response.usage.tokens_total,
            "cost_usd": result.avoided_cost_usd if result.cache_hit else response.usage.cost_usd,
            "cost_source": result.cost_source,
            "usage_json": json.dumps(result.usage_raw) if result.usage_raw is not None else None,
            "response_json": response.model_dump_json(),
            "error": result.error,
        },
    )

    raw_json = response.model_dump_json(indent=2)
    uploaded_data_url = f"data:{file.content_type};base64,{base64.b64encode(raw_bytes).decode()}"
    return templates.TemplateResponse(
        request,
        "extract.html",
        _extract_ctx(
            result=response,
            raw_json=raw_json,
            fixture_names=fixture_names,
            mock_mode=settings.mock_mode,
            uploaded_file_name=file.filename,
            uploaded_data_url=uploaded_data_url,
        ),
    )
