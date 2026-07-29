from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class ModelConfig(BaseModel):
    name: str
    price_per_1m_input: float
    price_per_1m_output: float
    # None = inherit the shared llm_base_url/llm_api_key below.
    base_url: str | None = None
    api_key: str | None = None
    # None = inherit Settings.llm_reasoning_effort below.
    reasoning_effort: str | None = None


_DEFAULT_MODELS = [
    ModelConfig(name="gemini-3.5-flash-lite", price_per_1m_input=0.30, price_per_1m_output=2.50)
]

_MODEL_SLOT_RE = re.compile(r"^LLM_MODEL_(\d+)$", re.IGNORECASE)


class LLMModelsSource(PydanticBaseSettingsSource):
    """Reads numbered `LLM_MODEL_<N>` / `_PRICE_IN` / `_PRICE_OUT` /
    `_BASE_URL` / `_API_KEY` env vars into `llm_models`. A custom source is
    needed because pydantic-settings' built-in env/dotenv sources only
    surface vars matching declared field names -- arbitrary `LLM_MODEL_1`,
    `LLM_MODEL_2`, ... aren't declared fields.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # unused; __call__ is overridden directly

    def __call__(self) -> dict[str, Any]:
        env_file = self.config.get("env_file")
        raw: dict[str, str | None] = {}
        if env_file:
            raw.update(dotenv_values(env_file))
        raw = {k.upper(): v for k, v in raw.items()}
        raw.update(os.environ)  # env wins over .env on conflicts

        slots: dict[int, str] = {}
        for key, value in raw.items():
            match = _MODEL_SLOT_RE.match(key)
            if match and value:
                slots[int(match.group(1))] = value

        if not slots:
            return {"llm_models": list(_DEFAULT_MODELS)}

        models = []
        for idx in sorted(slots):
            prefix = f"LLM_MODEL_{idx}"
            price_in = raw.get(f"{prefix}_PRICE_IN")
            price_out = raw.get(f"{prefix}_PRICE_OUT")
            models.append(
                ModelConfig(
                    name=slots[idx],
                    price_per_1m_input=float(price_in) if price_in else 0.0,
                    price_per_1m_output=float(price_out) if price_out else 0.0,
                    base_url=raw.get(f"{prefix}_BASE_URL"),
                    api_key=raw.get(f"{prefix}_API_KEY"),
                    reasoning_effort=raw.get(f"{prefix}_REASONING_EFFORT"),
                )
            )
        return {"llm_models": models}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- Mode ----
    mock_mode: bool = False
    app_timezone: str = "Asia/Ho_Chi_Minh"
    expose_usage_in_response: bool = True  # false = omit `usage` from /v1/extract responses

    # ---- Models ----
    # Shared endpoint/key, used by any model slot that doesn't set its own
    # base_url/api_key override. See LLM_MODEL_<N>_BASE_URL / _API_KEY below.
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str | None = None
    llm_models: list[ModelConfig] = Field(default_factory=lambda: list(_DEFAULT_MODELS))
    llm_timeout_s: float = 25.0
    llm_supports_structured_output: bool = True
    llm_retry_on_failure: bool = True
    llm_reasoning_effort: str = "low"  # "off"/"" = don't send the param at all
    openrouter_site_url: str | None = None
    openrouter_site_name: str | None = None

    # ---- Prompt ----
    prompt_file: str = "prompts/extract_v2.txt"
    prompt_version: str = "extract_v2"
    schema_version: str = "v2"

    # ---- Storage ----
    database_path: str = "data/app.db"
    cache_enabled: bool = True
    cache_ttl_days: int = 1  # 24h; 0 = never expires

    # ---- Currency ----
    # TEMPORARY: a fixed rate, not a live FX lookup. Revisit once there's a
    # real requirement for accurate VND figures (e.g. a rate API/cron).
    usd_to_vnd_rate: float = 26500.0

    # ---- Upload limits ----
    max_upload_bytes: int = 5 * 1024 * 1024  # 5 MB
    normalize_max_dimension: int = 2048
    normalize_jpeg_quality: int = 85
    pdf_render_dpi: int = 200

    # ---- Validation tolerances ----
    tolerance_relative: float = 0.005
    tolerance_absolute_floor_vnd: int = 1
    line_mismatch_ratio_trigger: float = 0.2
    confidence_weight_error: float = 0.25
    confidence_weight_warning: float = 0.08
    confidence_weight_info: float = 0.0
    status_threshold_usable: float = 0.75
    status_threshold_review: float = 0.35

    # ---- Auth ----
    api_key_required: bool = True  # false = /v1/extract never inspects X-API-Key at all

    # ---- Dashboard ----
    # No default: login always fails until this is explicitly set.
    dashboard_password: str | None = None
    # If unset, a random key is generated at startup (via default_factory,
    # evaluated once since Settings is cached) -- sessions just reset on
    # restart rather than falling back to an insecure fixed default.
    session_secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(32))

    # ---- Server ----
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    @property
    def prompt_text(self) -> str:
        return Path(self.prompt_file).read_text(encoding="utf-8")

    @property
    def default_model(self) -> ModelConfig:
        return self.llm_models[0]

    def resolve_model(self, name: str | None) -> ModelConfig:
        """Falls back to the default model on a missing/unrecognized name --
        an unknown X-Model header or dashboard dropdown value is never an
        error, per the confirmed product decision.
        """
        if name:
            for model in self.llm_models:
                if model.name == name:
                    return model
        return self.default_model

    def effective_reasoning_effort(self, model: ModelConfig) -> str | None:
        """None = omit the parameter entirely."""
        value = model.reasoning_effort
        if value is None:
            value = self.llm_reasoning_effort
        value = value.strip().lower()
        return None if value in ("", "off") else value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # init_settings (explicit constructor kwargs, used by tests) wins
        # over the numbered-slot source, matching normal pydantic-settings
        # precedence for every other field.
        return (
            init_settings,
            LLMModelsSource(settings_cls),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
