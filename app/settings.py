from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- Mode ----
    mock_mode: bool = False
    app_timezone: str = "Asia/Ho_Chi_Minh"
    expose_usage_in_response: bool = True  # false = omit `usage` from /v1/extract responses

    # ---- The one model ----
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str | None = None
    llm_model: str = "gemini-3.5-flash-lite"
    llm_price_per_1m_input: float = 0.30
    llm_price_per_1m_output: float = 2.50
    llm_timeout_s: float = 25.0
    llm_supports_structured_output: bool = True
    llm_retry_on_failure: bool = True
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
