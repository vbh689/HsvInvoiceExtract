# Invoice Extraction API — Integration Guide

For other applications/teams calling this service to turn a photo or PDF of a Vietnamese purchase invoice into structured JSON line items. This is extraction only — no SKU matching, no order management.

## Authentication

Whether `/v1/extract` and `/v1/purge-cache` require an `X-API-Key` header depends on the server's configuration (`API_KEY_REQUIRED`, default `false`). Check with the operator running your instance if you're unsure.

```
X-API-Key: hsv_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Your API key is issued by an admin** from the internal operator dashboard (`/dashboard` → API Keys). There is no self-service signup and no way to retrieve a key's plaintext after creation — if you lose it, ask an admin to revoke it and issue a new one. Keep it out of client-side code, logs, and version control; treat it like a password.

A missing key returns:

```json
{ "detail": "missing X-API-Key header" }
```

An unknown or revoked key returns:

```json
{ "detail": "invalid or revoked API key" }
```

Both are `HTTP 401`. When `API_KEY_REQUIRED=false`, `X-API-Key` is not inspected at all — even if you send one, it's ignored (the request simply isn't attributed to a key in the audit log). There is no other auth mechanism (no OAuth, no bearer tokens) and no anonymous access to the dashboard itself — `/healthz`, `/v1/models`, `/v1/stats`, `/docs`, and `/openapi.json` are the only always-open endpoints.

### `X-TenantCode` header (optional)

Every request may carry an `X-TenantCode` header — a separate dimension from the API key, used purely for tracking/reporting (per-tenant breakdowns in the dashboard). It has no bearing on authentication or authorization.

```
X-TenantCode: acme
```

Omit it and the request is attributed to tenant `"1"`.

### `X-UserName` header (optional)

Every request may also carry an `X-UserName` header, identifying the individual employee at the tenant who sent the request (tenant = company, `X-UserName` = person). Like `X-TenantCode`, it's audit-only — it has no bearing on authentication or authorization, and only affects what's shown in the dashboard's audit log.

```
X-UserName: nguyen.van.a
```

Unlike `X-TenantCode`, there is no default: omit it and the audit log records `NULL` for that request rather than a placeholder value.

### `X-Model` header (optional)

Selects which configured model handles the request. Omit it (or send a name the server doesn't recognize) and the request silently falls back to the server's default model — never an `HTTP` error.

```
X-Model: gemini-3.5-flash-lite
```

Call `GET /v1/models` (below) to discover which names are currently configured and which one is the default.

## Local testing (mock mode only)

`X-Mock-Fixture` is a test-only header for a server running with `MOCK_MODE=true`.
It selects the canned model response named
`tests/fixtures/mock_responses/<fixture>.json`, without contacting a model
provider. Omit the header to use the `default` fixture. When `MOCK_MODE=false`,
the server ignores this header.

```
X-Mock-Fixture: pretax_basic
```

Use this only against a local or otherwise isolated test deployment. It is not
part of the normal direct API integration flow described below.

## Endpoint

### `GET /v1/models`

No authentication required (like `/healthz`). Lists the models this deployment is configured to call, their fallback pricing, and which one is the default (used when `X-Model` is omitted or unrecognized).

```json
{
  "models": [
    { "name": "gemini-3.5-flash-lite", "price_per_1m_input": 0.30, "price_per_1m_output": 2.50, "is_default": true },
    { "name": "gpt-4o-mini", "price_per_1m_input": 0.15, "price_per_1m_output": 0.60, "is_default": false }
  ],
  "prompt_version": "extract_v2"
}
```

Note the response never includes provider endpoints or API keys, regardless of auth.

### `GET /v1/stats`

Aggregate usage for one tenant over a time period — request counts, status split, tokens, and cost. Read-only; it reports on requests already made and never triggers an extraction.

**Unauthenticated by design.** Unlike `/v1/models`, this *does* expose a tenant's volume and spend, and tenant codes are short and guessable (they start at `1`). Anyone who can reach the endpoint can read any tenant's figures. If that matters for your deployment, restrict `/v1/stats` at the reverse proxy — see `docs/OPERATIONS.md`.

| Query param | Type | Required | Notes |
|---|---|---|---|
| `tenant_code` | string | yes | The tenant to report on. Missing → `HTTP 422` |
| `user_name` | string | no | Narrows to one person at that tenant. Omit for the whole tenant |
| `period` | string | no | `today` (default), `yesterday`, `3d`, `7d`, `14d`, `30d`, `this_month`, `last_month`, `3mo`, `6mo`, `all`, `custom`. Unknown → `HTTP 400` |
| `start` / `end` | string | no | ISO datetimes bounding a `custom` period. Naive values are read as `APP_TIMEZONE` local time |

Day and month boundaries resolve in the server's `APP_TIMEZONE`, not UTC.

```bash
curl 'https://<host>/v1/stats?tenant_code=acme&period=30d'
```

```json
{
  "tenant_code": "acme",
  "user_name": null,
  "period": "30d",
  "start": "2026-06-30T00:00",
  "end": "2026-07-29T14:12",
  "request_count": 128,
  "status_counts": { "usable": 119, "needs_human_review": 8, "unusable": 1 },
  "avg_confidence": 0.94,
  "tokens_in": 412000,
  "tokens_out": 96000,
  "tokens_total": 508000,
  "cost_usd": 0.7412,
  "cost_vnd": 19642,
  "cache_hit_rate": 0.21,
  "first_used_at": "2026-06-30T02:11:04+00:00",
  "last_used_at": "2026-07-29T13:58:22+00:00"
}
```

- `start` / `end` — the resolved window, echoed back. Both `null` for `period=all`, which is unbounded.
- `request_count` — every logged request in the window, cache hits included.
- `tokens_*` / `cost_usd` — **billed** figures only: cache hits contributed nothing and are excluded, which is why they can be 0 while `request_count` is not.
- `cost_vnd` — `cost_usd` converted at the server's fixed `USD_TO_VND_RATE`, not a live FX rate.
- `cache_hit_rate` — fraction of `request_count` served from cache.
- `first_used_at` / `last_used_at` — `null` when the window has no requests.

### `POST /v1/extract`

Multipart form upload, one file per request.

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | file | yes | The invoice image or PDF |

**Supported `Content-Type` values:** `image/jpeg`, `image/png`, `image/webp`, `image/heic`, `image/heif`, `application/pdf`. Anything else returns `HTTP 415`:

```json
{ "detail": "unsupported content type: text/plain" }
```

**Limits:**
- Empty file body → `HTTP 400` (`{"detail": "empty file"}`)
- File larger than the configured max (`MAX_UPLOAD_BYTES`, 5 MB by default) → `HTTP 413` (`{"detail": "file too large"}`)

**Example (curl):**

```bash
curl -X POST https://<host>/v1/extract \
  -F "file=@invoice.jpg" \
  -H "X-API-Key: hsv_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  -H "X-TenantCode: acme" \
  -H "X-UserName: nguyen.van.a"
```

**Timing:** the request is synchronous — no polling, no webhook. With `LLM_RETRY_ON_FAILURE=true` (the default), a malformed or unreliable first response is retried once against the same model before the service gives up, so allow at least twice `LLM_TIMEOUT_S` in the caller's timeout budget. With `LLM_RETRY_ON_FAILURE=false`, the first dead response is returned immediately as `status: "unusable"` with `usage.attempts: 1`; budget for one provider timeout.

### `POST /v1/purge-cache`

Deletes **every** entry in the server's extraction cache and returns how many were removed. No request body, no parameters.

Requires `X-API-Key` on the same terms as `/v1/extract` — gated by `API_KEY_REQUIRED`, and open to anyone who can reach it when that's `false`. Any valid key can purge; keys are not scoped, so this clears the cache for every tenant and every model, not just yours.

```bash
curl -X POST https://<host>/v1/purge-cache \
  -H "X-API-Key: hsv_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

```json
{ "purged": 42 }
```

Nothing is lost by purging — the cache is a cost optimization, not a store of record. Every purged document simply gets re-extracted (and re-billed) the next time it's uploaded, and the audit log is untouched. Use it when a fix on the server side means previously cached results are no longer the answer you want; if the change was a prompt or schema version bump, the cache invalidates itself and you don't need this at all.

## Response

Always `HTTP 200` on a successful call (validation/reconciliation problems are surfaced *inside* the response body via `status` and `findings`, not as HTTP error codes). Real example, from a live extraction:

```json
{
  "request_id": "147765c5-a3a6-417b-817f-1a710f1231d0",
  "created_at": "2026-07-29T02:53:07.670779Z",
  "status": "usable",
  "confidence": 1.0,
  "price_basis": {
    "basis": "pre_tax",
    "resolved_by": "line_vs_subtotal",
    "agrees_with_model": true
  },
  "document": {
    "supplier_name": "Cong ty TNHH ACME",
    "supplier_tax_code": "0312345678",
    "invoice_number": "HD-000123",
    "invoice_date": "2026-03-15",
    "currency": "VND",
    "subtotal_printed": 350000,
    "subtotal_calculated": 350000,
    "tax_amount_printed": 35000,
    "tax_amount_calculated": 35000,
    "tax_rate": 0.1,
    "grand_total_printed": 385000,
    "grand_total_calculated": 385000,
    "discount": null
  },
  "line_items": [
    {
      "line_no": 1,
      "product_code": "SKU-001",
      "product_name": "Bulong M8",
      "unit": "cai",
      "quantity": 2.0,
      "unit_price_as_printed": 100000,
      "unit_price_pre_tax": 100000,
      "unit_price_post_tax": 110000,
      "line_total_pre_tax": 200000,
      "line_total_post_tax": 220000,
      "discount": null,
      "tax_rate": 0.1
    }
  ],
  "findings": [
    {
      "code": "TAX_RATE_INFERRED",
      "severity": "info",
      "message": "tax_rate not printed anywhere on the document; inferred as tax_amount / pre-tax base = 0.1",
      "field_path": "document.tax_rate",
      "line_no": null
    }
  ],
  "usage": {
    "cached": false,
    "model": "gemini-3.5-flash-lite",
    "attempts": 1,
    "latency_ms": 2109.99,
    "tokens_in": 1076,
    "tokens_out": 189,
    "tokens_total": 1265,
    "cost_usd": 0.0007953,
    "cost_vnd": 21,
    "prompt_version": "extract_v2",
    "schema_version": "v2"
  }
}
```

### The one field to branch on: `status`

Downstream code should key its logic off `status` alone; everything else in the response exists to justify or act on that value.

| `status` | Meaning |
|---|---|
| `usable` | High confidence, no unresolved errors. Safe to consume automatically. |
| `needs_human_review` | Confidence below the usable threshold, or at least one error-level finding (e.g. a mismatched line total, an unresolved price basis). Route to a human. |
| `unusable` | No line items were extracted, or confidence is below the review floor. Don't trust any field. |

### Field reference

- **`confidence`** (`0.0`–`1.0`) — derived from what reconciles arithmetically; never self-reported by the model.
- **`price_basis`** — whether the unit prices in `line_items` were resolved as pre-tax or post-tax, and how (`resolved_by`: `line_vs_subtotal`, `line_vs_grand_total`, `model_stated_fallback`, `tax_neutral`, or `insufficient_data`). You generally don't need this — every line item already carries **both** `unit_price_pre_tax` and `unit_price_post_tax`.
- **`document`** — header-level fields. `*_printed` is what's on the invoice; `*_calculated` is what the service computed from line items. Any field can be `null` if unreadable or absent — never guessed.
- **`line_items[]`** — one entry per row. `unit_price_as_printed` is always present as transcribed (even when basis couldn't be resolved), alongside the pre-tax/post-tax breakdown.
- **`findings[]`** — a single flattened array, structured, coded, severity-ranked (`info` / `warning` / `error`). Document-level findings have `line_no: null`; per-line findings carry the 1-based `line_no` they apply to instead of you having to parse `field_path`. See `app/schemas.py::FindingCode` for the full code list (e.g. `LINE_ARITHMETIC_MISMATCH`, `PRICE_BASIS_AMBIGUOUS`, `DOCUMENT_TOTALS_UNRECONCILABLE`, `MISSING_REQUIRED_FIELD`). Treat unknown future codes as informational — don't hard-fail on an unrecognized code.
- **`usage`** — one attempt or two. `attempts: 2` means the first response was malformed/unusable and the service retried once against the same model. `attempts: 1` can mean either that the first extraction was structurally valid or that retry was disabled and the first dead response was returned as unusable. Token counts and USD cost are totals across every completed provider response, including malformed JSON, schema-invalid output, and empty extractions; a network/application failure without provider telemetry adds no usage. Provider `tokens_total` values are summed directly rather than recalculated from input and output. `latency_ms` remains the final attempt's latency. `cost_vnd` is calculated once from the aggregate USD cost at the fixed configured rate (`settings.usd_to_vnd_rate`, currently 26500), not a live FX lookup. `cached: true` means an identical document was served without another model call, so the public `tokens_*` and costs are zero while the audit statistics retain the aggregate usage avoided. Set `EXPOSE_USAGE_IN_RESPONSE=false` to return `usage: null`; the aggregate is still recorded in the audit log/dashboard.
- **Money fields** (`subtotal_printed`, `unit_price_pre_tax`, etc.) are integers in VND — no decimal subunits, no float rounding to worry about. **`quantity`** and **`tax_rate`** are floats (quantities can be fractional, rates are naturally fractional, e.g. `0.1` = 10%).

## Error responses

| HTTP | Cause | Body |
|---|---|---|
| 400 | Empty file | `{"detail": "empty file"}` |
| 400 | Unknown `period` on `/v1/stats` | `{"detail": "unknown period: 'last_week'"}` |
| 401 | Missing API key (only when the server requires one) | `{"detail": "missing X-API-Key header"}` |
| 401 | Unknown or revoked API key (only when the server requires one) | `{"detail": "invalid or revoked API key"}` |
| 413 | File exceeds max upload size (5 MB default) | `{"detail": "file too large"}` |
| 415 | Unsupported `Content-Type` | `{"detail": "unsupported content type: <type>"}` |
| 422 | Missing `tenant_code` on `/v1/stats` | FastAPI validation-error body |

A `200` is never withheld because extraction went poorly — a bad or unreadable invoice still returns `200` with `status: "unusable"` and `findings` explaining why. HTTP-level errors only cover request-shape problems (auth, file type/size), not extraction quality.

## Practical guidance

- **Retry policy:** set `LLM_RETRY_ON_FAILURE=false` at startup to disable the automatic second call globally. This does not change which responses are considered dead; it only changes the maximum from two calls to one. For example, a blank image normally produces zero line items: retry enabled records two `zero_line_items` attempts before returning unusable, while retry disabled records one attempt and returns the same unusable status immediately.
- **Idempotency / re-uploads:** identical file bytes can use the same cache entry only after a non-unusable extraction has populated it. Re-uploading a cached successful/review result returns `usage.cached: true`; unusable results, including blank images, are not cached and can incur another provider call.
- **Polling/webhooks:** not needed — the call is synchronous end-to-end.
- **PDFs:** each request accepts one file. A multi-page PDF is rendered and processed as-is; if you need per-page results, split the PDF before uploading.
- **Health check:** `GET /healthz` (no auth) returns `{"status": "ok", "db": "ok"}` for liveness monitoring — it does not indicate whether an API key is valid.
