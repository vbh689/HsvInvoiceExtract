"""The extraction pipeline: normalize, cache, call with one retry, reconcile.

Every completed provider response contributes its usage to the request, even
when its content is malformed or otherwise unusable.  Call failures that do
not return provider telemetry contribute no invented usage.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
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


@dataclass
class AttemptRecord:
    attempt: int
    outcome: str
    call_result: LLMCallResult | None
    latency_ms: float
    error: str | None = None

    def usage_envelope(self) -> dict:
        envelope: dict = {"attempt": self.attempt, "outcome": self.outcome}
        if self.error is not None:
            envelope["error"] = self.error
        if self.call_result is not None:
            envelope["provider_usage"] = self.call_result.usage_raw
        return envelope


@dataclass
class RetryResult:
    raw: RawExtraction | None
    attempts: list[AttemptRecord]
    error: str | None


@dataclass
class AggregatedUsage:
    tokens_in: int
    tokens_out: int
    tokens_total: int
    tokens_cached: int
    cost_usd: float
    cost_source: str
    final_latency_ms: float
    usage_raw: dict


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
) -> RetryResult:
    """One call, with one retry (same model) on a dead attempt: a network
    error/timeout, non-JSON output, or output that fails RawExtraction
    validation, or zero line items. A low-confidence but structurally valid
    result is a real answer -- reconcile() communicates that via `status` --
    and does not retry.
    """
    attempts: list[AttemptRecord] = []
    last_error: str | None = None
    max_attempts = 2 if retry_on_failure else 1

    for attempt in range(1, max_attempts + 1):
        attempt_started = time.monotonic()
        try:
            call_result = await client.extract(images=images, prompt=prompt)
        except Exception as exc:  # network error, timeout, provider error
            last_error = str(exc)
            attempts.append(
                AttemptRecord(
                    attempt=attempt,
                    outcome="call_failed",
                    call_result=None,
                    latency_ms=(time.monotonic() - attempt_started) * 1000,
                    error=last_error,
                )
            )
        else:
            if call_result.content_error is not None or call_result.raw_json is None:
                last_error = call_result.content_error or "model output was not valid JSON"
                attempts.append(
                    AttemptRecord(
                        attempt=attempt,
                        outcome="invalid_json",
                        call_result=call_result,
                        latency_ms=call_result.latency_ms,
                        error=last_error,
                    )
                )
            else:
                try:
                    raw = RawExtraction.model_validate(call_result.raw_json)
                except ValidationError as exc:
                    last_error = str(exc)
                    attempts.append(
                        AttemptRecord(
                            attempt=attempt,
                            outcome="schema_invalid",
                            call_result=call_result,
                            latency_ms=call_result.latency_ms,
                            error=last_error,
                        )
                    )
                else:
                    if raw.line_items:
                        attempts.append(
                            AttemptRecord(
                                attempt=attempt,
                                outcome="success",
                                call_result=call_result,
                                latency_ms=call_result.latency_ms,
                            )
                        )
                        return RetryResult(raw=raw, attempts=attempts, error=None)
                    last_error = "extraction produced zero line items"
                    attempts.append(
                        AttemptRecord(
                            attempt=attempt,
                            outcome="zero_line_items",
                            call_result=call_result,
                            latency_ms=call_result.latency_ms,
                            error=last_error,
                        )
                    )

        if attempt == max_attempts:
            return RetryResult(raw=None, attempts=attempts, error=last_error)

    return RetryResult(raw=None, attempts=attempts, error=last_error)  # pragma: no cover


def _aggregate_usage(attempts: list[AttemptRecord]) -> AggregatedUsage:
    contributing = [attempt.call_result for attempt in attempts if attempt.call_result is not None]
    return AggregatedUsage(
        tokens_in=sum(call.tokens_in for call in contributing),
        tokens_out=sum(call.tokens_out for call in contributing),
        # Provider totals are deliberately summed independently.  They may not
        # equal input + output for every provider.
        tokens_total=sum(call.tokens_total for call in contributing),
        tokens_cached=sum(call.tokens_cached for call in contributing),
        cost_usd=sum(call.cost_usd for call in contributing),
        cost_source=(
            "provider"
            if contributing and all(call.cost_source == "provider" for call in contributing)
            else "computed"
        ),
        # Latency remains the final attempt's latency rather than a sum.
        final_latency_ms=attempts[-1].latency_ms if attempts else 0.0,
        usage_raw={"attempts": [attempt.usage_envelope() for attempt in attempts]},
    )


def _malformed_response(
    request_id: str,
    created_at: datetime,
    settings: Settings,
    usage: AggregatedUsage,
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
            latency_ms=usage.final_latency_ms,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            tokens_total=usage.tokens_total,
            cost_usd=usage.cost_usd,
            cost_vnd=usd_to_vnd(usage.cost_usd, settings.usd_to_vnd_rate),
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
            cached_audit = cached.get("_audit_usage") or {}
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
                tokens_cached=cached_audit.get("tokens_cached", 0),
                usage_raw=None,
                page_count=page_count,
                avoided_cost_usd=cached_response.usage.cost_usd,
                avoided_tokens_in=cached_response.usage.tokens_in,
                avoided_tokens_out=cached_response.usage.tokens_out,
                avoided_tokens_total=cached_response.usage.tokens_total,
            )

    retry_result = await _call_with_retry(
        client,
        images=normalized.pages,
        prompt=settings.prompt_text,
        retry_on_failure=settings.llm_retry_on_failure,
    )
    aggregated = _aggregate_usage(retry_result.attempts)
    raw = retry_result.raw
    attempts = len(retry_result.attempts)
    error = retry_result.error

    if raw is None:
        response = _malformed_response(
            request_id,
            created_at,
            settings,
            aggregated,
            attempts,
            error or "unknown error",
            model_name,
        )
        return PipelineResult(
            response=response,
            cache_hit=False,
            cost_source=aggregated.cost_source,
            tokens_cached=aggregated.tokens_cached,
            usage_raw=aggregated.usage_raw,
            page_count=page_count,
            error=error,
        )

    result = reconcile(raw, settings)
    confidence = compute_confidence(result.all_findings, settings)
    status = derive_status(confidence, result.all_findings, len(result.line_items), settings)

    final_call = retry_result.attempts[-1].call_result
    assert final_call is not None  # a successful attempt always has a provider response
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
            model=final_call.model,
            attempts=attempts,
            latency_ms=aggregated.final_latency_ms,
            tokens_in=aggregated.tokens_in,
            tokens_out=aggregated.tokens_out,
            tokens_total=aggregated.tokens_total,
            cost_usd=aggregated.cost_usd,
            cost_vnd=usd_to_vnd(aggregated.cost_usd, settings.usd_to_vnd_rate),
            prompt_version=settings.prompt_version,
            schema_version=settings.schema_version,
        ),
    )

    # A MALFORMED_MODEL_OUTPUT finding is only ever attached in the `raw is
    # None` branch above, which returns before reaching here -- so `status`
    # alone is sufficient to decide cacheability on this path.
    if settings.cache_enabled and status != "unusable":
        cache_payload = response.model_dump(mode="json")
        cache_payload["_audit_usage"] = {"tokens_cached": aggregated.tokens_cached}
        cache_set(db, cache_key, cache_payload)

    return PipelineResult(
        response=response,
        cache_hit=False,
        cost_source=aggregated.cost_source,
        tokens_cached=aggregated.tokens_cached,
        usage_raw=aggregated.usage_raw,
        page_count=page_count,
    )
