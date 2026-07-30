"""The one model: a provider-agnostic OpenAI-compatible Chat Completions
client, its fixture-backed mock twin for MOCK_MODE, and usage/cost parsing.
No ladder — one model, one retry (the retry loop lives in pipeline.py).
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openai import AsyncOpenAI

from app.schemas import RawExtraction
from app.settings import ModelConfig, Settings

SYSTEM_PROMPT = (
    "You are a meticulous document-transcription assistant for Vietnamese purchase invoices. "
    "You copy what is printed; you never calculate or guess."
)

DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mock_responses"
)


class MalformedModelOutputError(Exception):
    """Raised when the model's response can't be parsed as the expected JSON shape."""


@dataclass
class LLMCallResult:
    # `raw_json` is None when the provider returned content that was not valid
    # JSON.  The call still carries usage because providers can bill malformed
    # responses just like successful ones.
    raw_json: dict | None
    model: str
    latency_ms: float
    tokens_in: int
    tokens_out: int
    tokens_total: int
    tokens_cached: int
    cost_usd: float
    cost_source: str  # 'provider' | 'computed'
    usage_raw: dict
    content_error: str | None = None


class VisionExtractionClient(Protocol):
    async def extract(self, *, images: list[bytes], prompt: str) -> LLMCallResult: ...


def _to_data_url(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _parse_usage(usage, model: ModelConfig) -> tuple[int, int, int, int, float, str, dict]:
    raw = usage.model_dump() if usage else {}

    tokens_in = raw.get("prompt_tokens", 0)
    tokens_out = raw.get("completion_tokens", 0)
    tokens_total = raw.get("total_tokens", tokens_in + tokens_out)
    tokens_cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)

    # OpenRouter (and LiteLLM-style gateways) report real billed cost; Gemini doesn't.
    provider_cost = raw.get("cost")
    if provider_cost is not None:
        cost_usd, cost_source = float(provider_cost), "provider"
    else:
        cost_usd = (tokens_in / 1_000_000) * model.price_per_1m_input + (
            tokens_out / 1_000_000
        ) * model.price_per_1m_output
        cost_source = "computed"

    return tokens_in, tokens_out, tokens_total, tokens_cached, cost_usd, cost_source, raw


class OpenAICompatibleClient:
    def __init__(self, settings: Settings, model: ModelConfig) -> None:
        self._settings = settings
        self._model = model
        self._client = AsyncOpenAI(
            api_key=model.api_key or settings.llm_api_key or "unset",
            base_url=model.base_url or settings.llm_base_url,
        )
        self._extra_headers: dict[str, str] = {}
        if settings.openrouter_site_url:
            self._extra_headers["HTTP-Referer"] = settings.openrouter_site_url
        if settings.openrouter_site_name:
            self._extra_headers["X-OpenRouter-Title"] = settings.openrouter_site_name

    async def extract(self, *, images: list[bytes], prompt: str) -> LLMCallResult:
        settings = self._settings
        content: list[dict] = [{"type": "text", "text": prompt}]
        for image_bytes in images:
            content.append({"type": "image_url", "image_url": {"url": _to_data_url(image_bytes)}})

        kwargs: dict = {}
        if settings.llm_supports_structured_output:
            schema = RawExtraction.model_json_schema()
            # schema_version is pipeline metadata, not something on the document -- don't
            # ask the model to transcribe it. It has a pydantic default, so omitting it
            # from the advertised schema means an absent key still validates as "v2".
            schema["properties"].pop("schema_version", None)
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "invoice_extraction", "schema": schema, "strict": True},
            }
        if self._extra_headers:
            kwargs["extra_headers"] = self._extra_headers
        effort = settings.effective_reasoning_effort(self._model)
        if effort:
            kwargs["reasoning_effort"] = effort

        start = time.monotonic()
        response = await self._client.chat.completions.create(
            model=self._model.name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            timeout=settings.llm_timeout_s,
            **kwargs,
        )
        latency_ms = (time.monotonic() - start) * 1000

        # Capture billable telemetry before inspecting model content.  Invalid
        # JSON is retry-worthy, but it is still a completed provider response
        # whose usage must contribute to request totals.
        tokens_in, tokens_out, tokens_total, tokens_cached, cost_usd, cost_source, usage_raw = (
            _parse_usage(response.usage, self._model)
        )

        message = response.choices[0].message.content
        try:
            raw_json = json.loads(message)
        except (TypeError, ValueError) as exc:
            raw_json = None
            content_error = f"model output was not valid JSON: {exc}"
        else:
            content_error = None
        return LLMCallResult(
            raw_json=raw_json,
            model=self._model.name,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_total=tokens_total,
            tokens_cached=tokens_cached,
            cost_usd=cost_usd,
            cost_source=cost_source,
            usage_raw=usage_raw,
            content_error=content_error,
        )


class MockVisionClient:
    """Fixture-backed client for MOCK_MODE: no API key resolved, no network.
    Fixture selection is explicit (X-Mock-Fixture header / dashboard dropdown),
    never hash-based, so mock-mode requests stay deterministic.
    """

    def __init__(
        self,
        settings: Settings,
        fixture_name: str,
        model: ModelConfig,
        fixture_dir: Path | None = None,
    ):
        self._settings = settings
        self._fixture_name = fixture_name
        self._model = model
        self._dir = fixture_dir or DEFAULT_FIXTURE_DIR

    async def extract(self, *, images: list[bytes], prompt: str) -> LLMCallResult:
        path = self._dir / f"{self._fixture_name}.json"
        if not path.exists():
            raise MalformedModelOutputError(f"no mock fixture named '{self._fixture_name}'")
        raw_json = json.loads(path.read_text(encoding="utf-8"))

        # Deterministic small estimate for telemetry realism, not billing accuracy.
        tokens_in = len(prompt) // 4 + len(images) * 300
        tokens_out = len(json.dumps(raw_json)) // 4
        tokens_total = tokens_in + tokens_out
        cost_usd = (tokens_in / 1_000_000) * self._model.price_per_1m_input + (
            tokens_out / 1_000_000
        ) * self._model.price_per_1m_output

        return LLMCallResult(
            raw_json=raw_json,
            model=self._model.name,
            latency_ms=1.0,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_total=tokens_total,
            tokens_cached=0,
            cost_usd=cost_usd,
            cost_source="computed",
            usage_raw={
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
                "total_tokens": tokens_total,
            },
        )


def build_client(
    settings: Settings, *, fixture_name: str = "default", model_name: str | None = None
) -> VisionExtractionClient:
    model = settings.resolve_model(model_name)
    if settings.mock_mode:
        return MockVisionClient(settings, fixture_name, model)
    return OpenAICompatibleClient(settings, model)
