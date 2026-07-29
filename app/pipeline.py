"""extract(): the whole pipeline in one function -- normalize, check cache,
call the model (one retry on a dead attempt), reconcile, cache the result if
it's usable, and return. No orchestrator class, no ladder: one model, one
retry, one attempt worth keeping.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from app.db import cache_get, cache_set
from app.llm import LLMCallResult, VisionExtractionClient
from app.normalize.image import normalize_document
from app.schemas import (
    DocumentFields,
    ExtractionResponse,
    Finding,
    FindingCode,
    PriceBasisResolution,
    RawExtraction,
    Severity,
    UsageInfo,
)
from app.settings import Settings
from app.validation.confidence import compute_confidence, derive_status
from app.validation.reconcile import reconcile


@dataclass
class PipelineResult:
    response: ExtractionResponse
    cache_hit: bool
    cost_source: str  # 'provider' | 'computed' | 'cache_hit'
    tokens_cached: int
    usage_raw: dict | None
    page_count: int
    error: str | None = None
    # Populated only on a cache hit: what the original (non-cached) call that
    # populated this cache entry cost, for the "cost saved by cache" stat.
    # `response.usage` itself is always zeroed on a cache hit per the API
    # contract ("cache hits cost nothing").
    avoided_cost_usd: float = 0.0
    avoided_tokens_in: int = 0
    avoided_tokens_out: int = 0
    avoided_tokens_total: int = 0


def compute_cache_key(
    content_hash: str,
    prompt_version: str,
    schema_version: str,
    model_name: str,
    reasoning_effort: str,
) -> str:
    key = f"{content_hash}:{prompt_version}:{schema_version}:{model_name}:{reasoning_effort}"
    return hashlib.sha256(key.encode()).hexdigest()


def usd_to_vnd(cost_usd: float, rate: float) -> int:
    return round(cost_usd * rate)


async def _call_with_retry(
    client: VisionExtractionClient, *, images: list[bytes], prompt: str, retry_on_failure: bool
) -> tuple[LLMCallResult | None, RawExtraction | None, int, str | None]:
    """One call, with one retry (same model) on a dead attempt: a network
    error/timeout, non-JSON output, or output that fails RawExtraction
    validation, or zero line items. A low-confidence but structurally valid
    result is a real answer -- reconcile() communicates that via `status` --
    and does not retry.
    """
    attempts = 0
    last_error: str | None = None
    max_attempts = 2 if retry_on_failure else 1

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            call_result = await client.extract(images=images, prompt=prompt)
        except Exception as exc:  # network error, timeout, provider error
            last_error = str(exc)
            call_result = None
        else:
            try:
                raw = RawExtraction.model_validate(call_result.raw_json)
            except ValidationError as exc:
                last_error = str(exc)
            else:
                if raw.line_items:
                    return call_result, raw, attempts, None
                last_error = "extraction produced zero line items"

        if attempt == max_attempts:
            return call_result, None, attempts, last_error

    return None, None, attempts, last_error  # pragma: no cover - loop always returns above


def _malformed_response(
    request_id: str,
    created_at: datetime,
    settings: Settings,
    call_result: LLMCallResult | None,
    attempts: int,
    error: str,
    model_name: str,
) -> ExtractionResponse:
    finding = Finding(
        code=FindingCode.MALFORMED_MODEL_OUTPUT, severity=Severity.ERROR, message=error
    )
    return ExtractionResponse(
        request_id=request_id,
        created_at=created_at,
        status="unusable",
        confidence=0.0,
        price_basis=PriceBasisResolution(basis="unknown", resolved_by="insufficient_data"),
        document=DocumentFields(),
        line_items=[],
        findings=[finding],
        usage=UsageInfo(
            cached=False,
            model=model_name,
            attempts=attempts,
            latency_ms=call_result.latency_ms if call_result else 0.0,
            tokens_in=call_result.tokens_in if call_result else 0,
            tokens_out=call_result.tokens_out if call_result else 0,
            tokens_total=call_result.tokens_total if call_result else 0,
            cost_usd=call_result.cost_usd if call_result else 0.0,
            cost_vnd=usd_to_vnd(
                call_result.cost_usd if call_result else 0.0, settings.usd_to_vnd_rate
            ),
            prompt_version=settings.prompt_version,
            schema_version=settings.schema_version,
        ),
    )


async def extract(
    *,
    raw_bytes: bytes,
    content_type: str,
    settings: Settings,
    client: VisionExtractionClient,
    db: sqlite3.Connection,
    model_name: str,
    reasoning_effort: str | None,
) -> PipelineResult:
    request_id = str(uuid.uuid4())
    created_at = datetime.now(UTC)

    normalized = normalize_document(
        raw_bytes,
        content_type,
        max_dimension=settings.normalize_max_dimension,
        jpeg_quality=settings.normalize_jpeg_quality,
        pdf_render_dpi=settings.pdf_render_dpi,
    )
    page_count = len(normalized.pages)
    cache_key = compute_cache_key(
        normalized.content_hash,
        settings.prompt_version,
        settings.schema_version,
        model_name,
        reasoning_effort or "off",
    )

    if settings.cache_enabled:
        cached = cache_get(db, cache_key, ttl_days=settings.cache_ttl_days)
        if cached is not None:
            cached_response = ExtractionResponse.model_validate(cached)
            zeroed_usage = cached_response.usage.model_copy(
                update={
                    "cached": True,
                    "attempts": 0,
                    "latency_ms": 0.0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "tokens_total": 0,
                    "cost_usd": 0.0,
                    "cost_vnd": 0,
                }
            )
            response = cached_response.model_copy(
                update={
                    "request_id": request_id,
                    "created_at": created_at,
                    "usage": zeroed_usage,
                }
            )
            return PipelineResult(
                response=response,
                cache_hit=True,
                cost_source="cache_hit",
                tokens_cached=0,
                usage_raw=None,
                page_count=page_count,
                avoided_cost_usd=cached_response.usage.cost_usd,
                avoided_tokens_in=cached_response.usage.tokens_in,
                avoided_tokens_out=cached_response.usage.tokens_out,
                avoided_tokens_total=cached_response.usage.tokens_total,
            )

    call_result, raw, attempts, error = await _call_with_retry(
        client,
        images=normalized.pages,
        prompt=settings.prompt_text,
        retry_on_failure=settings.llm_retry_on_failure,
    )

    if raw is None:
        response = _malformed_response(
            request_id,
            created_at,
            settings,
            call_result,
            attempts,
            error or "unknown error",
            model_name,
        )
        return PipelineResult(
            response=response,
            cache_hit=False,
            cost_source=call_result.cost_source if call_result else "computed",
            tokens_cached=call_result.tokens_cached if call_result else 0,
            usage_raw=call_result.usage_raw if call_result else None,
            page_count=page_count,
            error=error,
        )

    result = reconcile(raw, settings)
    confidence = compute_confidence(result.all_findings, settings)
    status = derive_status(confidence, result.all_findings, len(result.line_items), settings)

    response = ExtractionResponse(
        request_id=request_id,
        created_at=created_at,
        status=status,
        confidence=confidence,
        price_basis=result.price_basis,
        document=result.document,
        line_items=result.line_items,
        findings=result.all_findings,
        usage=UsageInfo(
            cached=False,
            model=call_result.model,
            attempts=attempts,
            latency_ms=call_result.latency_ms,
            tokens_in=call_result.tokens_in,
            tokens_out=call_result.tokens_out,
            tokens_total=call_result.tokens_total,
            cost_usd=call_result.cost_usd,
            cost_vnd=usd_to_vnd(call_result.cost_usd, settings.usd_to_vnd_rate),
            prompt_version=settings.prompt_version,
            schema_version=settings.schema_version,
        ),
    )

    # A MALFORMED_MODEL_OUTPUT finding is only ever attached in the `raw is
    # None` branch above, which returns before reaching here -- so `status`
    # alone is sufficient to decide cacheability on this path.
    if settings.cache_enabled and status != "unusable":
        cache_set(db, cache_key, response.model_dump(mode="json"))

    return PipelineResult(
        response=response,
        cache_hit=False,
        cost_source=call_result.cost_source,
        tokens_cached=call_result.tokens_cached,
        usage_raw=call_result.usage_raw,
        page_count=page_count,
    )
