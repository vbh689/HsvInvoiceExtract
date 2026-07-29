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
POST /v1/extract (gated by X-API-Key)
  -> normalize (EXIF correction, downscale to JPEG, PDF->image render)
  -> cache (sha256 of normalized bytes + prompt_version + schema_version)
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
| `app/api.py` | `POST /v1/extract`, `GET /healthz` |
| `app/dashboard.py` | All `/dashboard` routes — session-cookie login, overview + charts, logs, keys, per-key stats, operator test-upload |
| `app/stats.py` | Time-period resolution + SQL aggregation queries backing the dashboard |

Everything the runtime reads lives under `app/` or `prompts/` — nothing else
is needed inside the container.

## Quickstart

```bash
uv sync --group dev

# Run the whole test suite offline (mock mode, no API keys, no network)
uv run pytest

# Lint
uv run ruff check .
uv run ruff format --check .

# Run the service
cp .env.example .env   # edit LLM_API_KEY / DASHBOARD_PASSWORD as needed
uv run uvicorn app.main:app --reload
```

### Try it in mock mode

```bash
MOCK_MODE=true DASHBOARD_PASSWORD=devpass uv run uvicorn app.main:app --reload
```

`/v1/extract` always requires a valid API key. Log into the dashboard at
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

Set `MOCK_MODE=false` and fill in `LLM_BASE_URL` / `LLM_API_KEY` /
`LLM_MODEL` in `.env` (see `.env.example` for two worked examples: Gemini
and OpenRouter). Any OpenAI-compatible Chat Completions endpoint works — the
client (`app/llm.py`) never hardcodes a provider.

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
