# Invoice Extraction Service (Lite)

Turns a photo or PDF of a Vietnamese purchase invoice (hóa đơn nhập hàng)
into structured JSON line items. Extraction only — no SKU matching, no order
management. Another team consumes the JSON downstream via `POST
/v1/extract`, gated by an API key. The one UI is an internal operator
dashboard (`/dashboard`) for observing pipeline activity and managing API
keys — separate from the extraction pipeline and from the downstream JSON
contract.

This is a from-scratch, deliberately smaller rebuild of
`HsvImportOrderService5` — same validated extraction logic, much simpler
plumbing (one model + one retry instead of a 5-rung escalation ladder, a
flat `.env`-only `Settings` instead of YAML + pydantic config trees, one
SQLite file instead of three swappable Protocol+filesystem backends). See
`SPEC-LITE.md` in the parent repo for the full rationale.

## The core problem

The unit-price column on a Vietnamese invoice may be pre-tax or post-tax
depending on the supplier, and a vision model cannot be trusted to know
which. So this service is built around one rule:

**The model transcribes; it never computes.** It copies numbers exactly as
printed (including "." vs "," formatting) and never multiplies, sums, or
converts between tax bases. Every calculation — tax-basis inference,
line/subtotal/grand-total reconciliation, confidence scoring — happens in a
deterministic validation layer (`app/validation/`), because a calculation
can be verified and a model's arithmetic cannot.

Tax basis is inferred from arithmetic: if line totals sum to the printed
subtotal, prices are pre-tax; if they sum to the printed grand total,
they're post-tax. The model's own opinion (`stated_price_basis`) is advisory
only — if it disagrees with the arithmetic, the arithmetic wins and the
conflict is reported as a finding. Both pre-tax and post-tax figures are
always emitted per line, so the downstream consumer never has to convert
anything. Confidence is earned from what reconciles, never self-reported by
the model. Unreadable or absent fields stay `null` rather than being
guessed.

## Pipeline

```
POST /v1/extract (gated by X-API-Key, unless API_KEY_REQUIRED=false)
  -> resolve model (X-Model header; an unknown name falls back to the default)
  -> normalize (EXIF correction, downscale to JPEG, PDF->image render)
  -> cache (sha256 of normalized bytes + prompt_version + schema_version
            + model name + reasoning effort)
  -> call model; one retry on a dead/malformed attempt only
      -> validate & reconcile (app/validation/)
  -> response (always the best attempt, never empty)
  -> request log (SQLite, powers the dashboard and the audit trail)
```

Synchronous, no polling or webhooks. See `docs/API.md` for the full response
contract and `app/pipeline.py::extract()` for the whole flow in one
function.

## Architecture in one screen

| Module | Role |
|---|---|
| `app/settings.py` | One flat `pydantic-settings` class, `.env`-only, no YAML |
| `app/db.py` | stdlib `sqlite3` — schema, connection, every query. No ORM. |
| `app/llm.py` | OpenAI-compatible client (any provider), usage/cost parsing, fixture-backed mock client |
| `app/pipeline.py` | `extract()` — normalize → cache → call → retry → reconcile → persist |
| `app/schemas.py` | `RawExtraction` (model-transcribed strings) vs `ExtractionResponse` (API contract with computed ints) |
| `app/normalize/` | Image/PDF normalization + deterministic VN number parsing |
| `app/validation/` | Reconciliation (`reconcile.py`) and confidence/status derivation (`confidence.py`) — the validated core, ported near-verbatim |
| `app/api.py` | `POST /v1/extract`, `POST /v1/purge-cache`, `GET /healthz`, `GET /v1/models`, `GET /v1/stats` |
| `app/dashboard.py` | All `/dashboard` routes — session-cookie login, overview + charts, statistics + CSV exports, logs, keys, per-key stats, tenants, cache purge, operator test-upload, rendered API docs |
| `app/stats.py` | Time-period resolution + SQL aggregation queries backing the dashboard |

Everything the runtime reads lives under `app/`, `prompts/`, `docs/API.md`
(rendered at `/dashboard/api-docs`) or `tests/fixtures/mock_responses/`
(mock mode) — those four are exactly what the Dockerfile copies, and nothing
else is needed inside the container.

## Quickstart

```bash
uv sync --group dev

# Run the whole test suite offline (mock mode, no API keys, no network)
uv run pytest

# Lint
uv run ruff check .
uv run ruff format --check .

# Run the service
# `python -m app.main` (not `uvicorn app.main:app`) so HOST/PORT/LOG_LEVEL
# from .env are honored instead of uvicorn's own hardcoded defaults.
cp .env.example .env   # edit LLM_API_KEY / DASHBOARD_PASSWORD as needed
uv run python -m app.main
```

### Try it in mock mode

```bash
MOCK_MODE=true DASHBOARD_PASSWORD=devpass uv run python -m app.main
```

`/v1/extract` requires a valid API key unless `API_KEY_REQUIRED=false` (in
which case `X-API-Key` isn't inspected at all). Log into the dashboard at
`http://localhost:8000/dashboard/login` with `DASHBOARD_PASSWORD`, create a
key from the API Keys page, then:

```bash
curl -X POST http://localhost:8000/v1/extract \
  -F "file=@sample.jpg" \
  -H "X-API-Key: hsv_..." \
  -H "X-Mock-Fixture: pretax_basic"
```

`X-Mock-Fixture` selects a fixture from `tests/fixtures/mock_responses/*.json`
and is only honored when `MOCK_MODE=true` — ignored entirely in production.
Omit it and the service falls back to `default.json`, a clean fully-
reconciling invoice, so a mock-mode request always gets a sensible response.

### Run against a real provider

Set `MOCK_MODE=false` and fill in `LLM_BASE_URL` / `LLM_API_KEY` plus at
least one numbered model slot — `LLM_MODEL_1` with its `_PRICE_IN` /
`_PRICE_OUT`, and optional `_BASE_URL` / `_API_KEY` / `_REASONING_EFFORT`
overrides (there is no bare `LLM_MODEL` var). See `.env.example` for worked
Gemini, OpenRouter and OpenAI slots. `LLM_MODEL_1` is the default model;
callers pick another with the `X-Model` header, and `GET /v1/models` lists
what's configured. Any OpenAI-compatible Chat Completions endpoint works —
the client (`app/llm.py`) never hardcodes a provider.

## Docs

- `docs/API.md` — the integration contract for downstream consumers: auth,
  request/response shape, error table, retry/idempotency guidance.
- `docs/OPERATIONS.md` — deploy, environment variable reference, backup
  (there is exactly one file to back up: `data/app.db`), API key rotation.
- `/docs` — Swagger UI (open, schema only, with a working Authorize button).

## Deployment

Docker + `docker compose`, built and deployed same-host by Jenkins
(`Jenkinsfile`) — no registry push. `docs/OPERATIONS.md` has the full
runbook; short version:

```bash
# on the deploy host, once, by hand -- Jenkins never touches .env
cp .env.example .env && vim .env
mkdir -p data && chown -R 1000:1000 data
```

`GET /healthz` runs a `SELECT 1` and is used for both the container's own
`HEALTHCHECK` and the pipeline's post-deploy smoke test.

## Out of scope

SKU matching, order management, multi-model routing/fallback, async or
queued processing. This service extracts; it does not decide what to do
with the result.
