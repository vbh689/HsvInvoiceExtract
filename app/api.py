"""POST /v1/extract and GET /healthz."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.security import APIKeyHeader

from app.db import insert_request, verify_api_key
from app.llm import build_client
from app.normalize.image import SUPPORTED_CONTENT_TYPES, detect_content_type
from app.pipeline import extract as run_pipeline
from app.schemas import ExtractionResponse
from app.settings import Settings

router = APIRouter()

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(
    request: Request, api_key: str | None = Depends(_api_key_header)
) -> sqlite3.Row:
    if not api_key:
        raise HTTPException(status_code=401, detail="missing X-API-Key header")
    row = verify_api_key(request.app.state.db, api_key)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid or revoked API key")
    return row


def _resolve_content_type(declared: str | None, raw_bytes: bytes) -> str:
    if declared in SUPPORTED_CONTENT_TYPES:
        return declared
    detected = detect_content_type(raw_bytes)
    if detected in SUPPORTED_CONTENT_TYPES:
        return detected
    raise HTTPException(status_code=415, detail=f"unsupported content type: {declared}")


@router.get("/healthz")
def healthz(request: Request) -> dict:
    try:
        request.app.state.db.execute("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "error"
    return {"status": "ok", "db": db_status}


@router.post("/v1/extract", response_model=ExtractionResponse)
async def extract_endpoint(
    request: Request,
    file: UploadFile = File(...),
    x_mock_fixture: str | None = Header(default=None, alias="X-Mock-Fixture"),
    api_key_row: sqlite3.Row = Depends(require_api_key),
) -> ExtractionResponse:
    settings: Settings = request.app.state.settings
    db: sqlite3.Connection = request.app.state.db

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="empty file")
    if len(raw_bytes) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="file too large")

    content_type = _resolve_content_type(file.content_type, raw_bytes)

    fixture_name = x_mock_fixture if settings.mock_mode and x_mock_fixture else "default"
    client = build_client(settings, fixture_name=fixture_name)

    result = await run_pipeline(
        raw_bytes=raw_bytes,
        content_type=content_type,
        settings=settings,
        client=client,
        db=db,
    )
    response = result.response
    document = response.document

    # Cache hits report zeroed usage in the client-facing response ("cache
    # hits cost nothing"), but the audit log records what the call avoided
    # -- that's the number behind the dashboard's "cost saved by cache" tile.
    if result.cache_hit:
        tokens_in, tokens_out, tokens_total = (
            result.avoided_tokens_in,
            result.avoided_tokens_out,
            result.avoided_tokens_total,
        )
        cost_usd = result.avoided_cost_usd
    else:
        tokens_in, tokens_out, tokens_total = (
            response.usage.tokens_in,
            response.usage.tokens_out,
            response.usage.tokens_total,
        )
        cost_usd = response.usage.cost_usd

    insert_request(
        db,
        {
            "request_id": response.request_id,
            "created_at": response.created_at.isoformat(),
            "api_key_id": api_key_row["id"],
            "api_key_label": api_key_row["label"],
            "source": "api",
            "filename": file.filename,
            "content_type": content_type,
            "file_bytes": len(raw_bytes),
            "page_count": result.page_count,
            "status": response.status,
            "confidence": response.confidence,
            "price_basis": response.price_basis.basis,
            "line_count": len(response.line_items),
            "grand_total": document.grand_total_calculated
            if document.grand_total_calculated is not None
            else document.grand_total_printed,
            "cache_hit": int(result.cache_hit),
            "model": response.usage.model,
            "attempt_count": response.usage.attempts,
            "latency_ms": response.usage.latency_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_cached": result.tokens_cached,
            "tokens_total": tokens_total,
            "cost_usd": cost_usd,
            "cost_source": result.cost_source,
            "usage_json": json.dumps(result.usage_raw) if result.usage_raw is not None else None,
            "response_json": response.model_dump_json(),
            "error": result.error,
        },
    )

    if not settings.expose_usage_in_response:
        response = response.model_copy(update={"usage": None})
    return response
