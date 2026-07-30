from datetime import UTC
from io import BytesIO

import pytest
from PIL import Image

from app.db import cache_get, get_connection, init_db
from app.llm import LLMCallResult
from app.pipeline import compute_cache_key, extract
from app.settings import Settings

VALID_RAW_JSON = {
    "supplier_name": "Cong ty TNHH Vat Tu Thanh Cong",
    "supplier_tax_code": "0301234567",
    "invoice_number": "HD-000001",
    "invoice_date": "01/07/2026",
    "currency": "VND",
    "printed_subtotal": "1.500.000",
    "printed_tax_amount": "150.000",
    "printed_tax_rate": "10%",
    "printed_grand_total": "1.650.000",
    "printed_discount": None,
    "stated_price_basis": "pre_tax",
    "line_items": [
        {
            "line_no": "1",
            "product_code": "SP001",
            "product_name": "Giay A4",
            "unit": "thung",
            "quantity": "5",
            "unit_price": "200.000",
            "line_total": "1.000.000",
            "discount": None,
            "tax_rate": "10%",
        },
        {
            "line_no": "2",
            "product_code": "SP002",
            "product_name": "But bi xanh",
            "unit": "hop",
            "quantity": "10",
            "unit_price": "50.000",
            "line_total": "500.000",
            "discount": None,
            "tax_rate": "10%",
        },
    ],
    "model_notes": None,
}


def _call_result(raw_json: dict | None, **overrides) -> LLMCallResult:
    values = {
        "raw_json": raw_json,
        "model": "fake-model",
        "latency_ms": 1.0,
        "tokens_in": 10,
        "tokens_out": 5,
        "tokens_total": 15,
        "tokens_cached": 0,
        "cost_usd": 0.001,
        "cost_source": "computed",
        "usage_raw": {},
        "content_error": None,
    }
    values.update(overrides)
    return LLMCallResult(**values)


class FakeClient:
    """Scripted client: pops one behavior per call from a queue."""

    def __init__(self, behaviors: list):
        self._behaviors = list(behaviors)
        self.calls = 0

    async def extract(self, *, images, prompt):
        self.calls += 1
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


@pytest.fixture
def db(tmp_path):
    settings = Settings(database_path=str(tmp_path / "app.db"))
    conn = get_connection(settings)
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def image_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (20, 10), color=(1, 2, 3)).save(buf, format="JPEG")
    return buf.getvalue()


async def _run(settings, client, db, image_bytes, model_name="fake-model", reasoning_effort=None):
    return await extract(
        raw_bytes=image_bytes,
        content_type="image/jpeg",
        settings=settings,
        client=client,
        db=db,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )


async def test_retry_succeeds_on_second_attempt(db, image_bytes):
    settings = Settings(llm_retry_on_failure=True)
    client = FakeClient([ConnectionError("boom"), _call_result(VALID_RAW_JSON)])

    result = await _run(settings, client, db, image_bytes)

    assert client.calls == 2
    assert result.response.usage.attempts == 2
    assert result.response.status == "usable"


async def test_retry_gives_up_after_two_dead_attempts(db, image_bytes):
    settings = Settings(llm_retry_on_failure=True)
    client = FakeClient([ConnectionError("boom"), ConnectionError("boom again")])

    result = await _run(settings, client, db, image_bytes)

    assert client.calls == 2
    assert result.response.status == "unusable"
    assert result.error is not None


async def test_zero_line_items_triggers_retry(db, image_bytes):
    settings = Settings(llm_retry_on_failure=True)
    empty_lines = dict(VALID_RAW_JSON, line_items=[])
    client = FakeClient([_call_result(empty_lines), _call_result(VALID_RAW_JSON)])

    result = await _run(settings, client, db, image_bytes)

    assert client.calls == 2
    assert result.response.status == "usable"


@pytest.mark.parametrize(
    ("first_result", "expected_outcome"),
    [
        (_call_result(dict(VALID_RAW_JSON, line_items=[])), "zero_line_items"),
        (_call_result({"line_items": "not-a-list"}), "schema_invalid"),
        (
            _call_result(
                None,
                content_error="model output was not valid JSON: bad content",
            ),
            "invalid_json",
        ),
    ],
)
async def test_retry_aggregates_usage_from_unusable_response(
    db, image_bytes, first_result, expected_outcome
):
    first_result.tokens_in = 11
    first_result.tokens_out = 7
    first_result.tokens_total = 21
    first_result.tokens_cached = 3
    first_result.cost_usd = 0.002
    first_result.latency_ms = 90.0
    first_result.usage_raw = {"prompt_tokens": 11, "total_tokens": 21}
    second = _call_result(
        VALID_RAW_JSON,
        tokens_in=13,
        tokens_out=8,
        tokens_total=25,
        tokens_cached=4,
        cost_usd=0.003,
        latency_ms=45.0,
        usage_raw={"prompt_tokens": 13, "total_tokens": 25},
    )
    client = FakeClient([first_result, second])

    result = await _run(Settings(llm_retry_on_failure=True), client, db, image_bytes)

    usage = result.response.usage
    assert usage.attempts == 2
    assert (usage.tokens_in, usage.tokens_out, usage.tokens_total) == (24, 15, 46)
    assert usage.cost_usd == pytest.approx(0.005)
    assert usage.cost_vnd == round(0.005 * Settings().usd_to_vnd_rate)
    assert usage.latency_ms == 45.0
    assert result.tokens_cached == 7
    assert result.usage_raw["attempts"][0]["outcome"] == expected_outcome
    assert result.usage_raw["attempts"][1]["outcome"] == "success"


async def test_network_failure_contributes_no_usage(db, image_bytes):
    success = _call_result(
        VALID_RAW_JSON,
        tokens_in=13,
        tokens_out=8,
        tokens_total=25,
        tokens_cached=4,
        cost_usd=0.003,
        latency_ms=45.0,
        usage_raw={"prompt_tokens": 13},
    )
    client = FakeClient([ConnectionError("boom"), success])

    result = await _run(Settings(llm_retry_on_failure=True), client, db, image_bytes)

    usage = result.response.usage
    assert (usage.tokens_in, usage.tokens_out, usage.tokens_total) == (13, 8, 25)
    assert usage.cost_usd == pytest.approx(0.003)
    failed_attempt = result.usage_raw["attempts"][0]
    assert failed_attempt["outcome"] == "call_failed"
    assert "provider_usage" not in failed_attempt


async def test_two_unusable_responses_are_both_aggregated(db, image_bytes):
    first = _call_result(
        dict(VALID_RAW_JSON, line_items=[]),
        tokens_in=3,
        tokens_out=5,
        tokens_total=10,
        cost_usd=0.001,
        latency_ms=12.0,
    )
    second = _call_result(
        {"line_items": "invalid"},
        tokens_in=7,
        tokens_out=11,
        tokens_total=20,
        cost_usd=0.002,
        latency_ms=34.0,
    )

    result = await _run(
        Settings(llm_retry_on_failure=True), FakeClient([first, second]), db, image_bytes
    )

    assert result.response.status == "unusable"
    assert result.response.usage.tokens_total == 30
    assert result.response.usage.cost_usd == pytest.approx(0.003)
    assert result.response.usage.latency_ms == 34.0


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (("provider", "provider"), "provider"),
        (("computed", "computed"), "computed"),
        (("provider", "computed"), "computed"),
    ],
)
async def test_aggregate_cost_source(db, image_bytes, sources, expected):
    first = _call_result(
        dict(VALID_RAW_JSON, line_items=[]), cost_source=sources[0], cost_usd=0.004
    )
    second = _call_result(VALID_RAW_JSON, cost_source=sources[1], cost_usd=0.006)

    result = await _run(
        Settings(llm_retry_on_failure=True), FakeClient([first, second]), db, image_bytes
    )

    assert result.cost_source == expected
    assert result.response.usage.cost_usd == pytest.approx(0.01)


async def test_low_confidence_valid_result_does_not_retry(db, image_bytes):
    settings = Settings(llm_retry_on_failure=True)
    # Grand total contradicts the line items -> DOCUMENT_TOTALS_UNRECONCILABLE,
    # a real (if bad) answer. This must not consume the retry budget.
    bad_totals = dict(VALID_RAW_JSON, printed_grand_total="9.999.999", printed_subtotal="9.999.999")
    client = FakeClient([_call_result(bad_totals)])

    result = await _run(settings, client, db, image_bytes)

    assert client.calls == 1
    assert result.response.usage.attempts == 1


async def test_retry_disabled_by_setting(db, image_bytes):
    settings = Settings(llm_retry_on_failure=False)
    client = FakeClient([ConnectionError("boom")])

    result = await _run(settings, client, db, image_bytes)

    assert client.calls == 1
    assert result.response.status == "unusable"


async def test_usable_result_is_cached_and_served_on_second_call(db, image_bytes):
    settings = Settings(cache_enabled=True)
    client = FakeClient([_call_result(VALID_RAW_JSON, tokens_cached=4)])

    first = await _run(settings, client, db, image_bytes)
    assert first.cache_hit is False
    assert client.calls == 1

    second = await _run(settings, client, db, image_bytes)
    assert second.cache_hit is True
    assert second.response.usage.cached is True
    assert second.tokens_cached == 4
    assert client.calls == 1  # model not called again


async def test_unusable_result_is_not_cached(db, image_bytes):
    settings = Settings(cache_enabled=True, llm_retry_on_failure=False)
    client = FakeClient([ConnectionError("boom"), _call_result(VALID_RAW_JSON)])

    first = await _run(settings, client, db, image_bytes)
    assert first.response.status == "unusable"

    # Same bytes -> same cache key. Since the failed attempt was never
    # cached, the second call must hit the model again rather than replay
    # the poisoned failure.
    second = await _run(settings, client, db, image_bytes)
    assert second.cache_hit is False
    assert second.response.status == "usable"
    assert client.calls == 2


async def test_cache_disabled_never_stores_or_reads(db, image_bytes):
    settings = Settings(cache_enabled=False)
    client = FakeClient([_call_result(VALID_RAW_JSON), _call_result(VALID_RAW_JSON)])

    await _run(settings, client, db, image_bytes)
    second = await _run(settings, client, db, image_bytes)

    assert second.cache_hit is False
    assert client.calls == 2


def test_compute_cache_key_is_deterministic():
    a = compute_cache_key("abc123", "extract_v2", "v2", "gemini-3.5-flash-lite", "low")
    b = compute_cache_key("abc123", "extract_v2", "v2", "gemini-3.5-flash-lite", "low")
    c = compute_cache_key("abc123", "extract_v2", "v3", "gemini-3.5-flash-lite", "low")
    assert a == b
    assert a != c


def test_compute_cache_key_differs_by_model():
    a = compute_cache_key("abc123", "extract_v2", "v2", "gemini-3.5-flash-lite", "low")
    b = compute_cache_key("abc123", "extract_v2", "v2", "gpt-4o-mini", "low")
    assert a != b


def test_compute_cache_key_differs_by_reasoning_effort():
    a = compute_cache_key("abc123", "extract_v2", "v2", "gemini-3.5-flash-lite", "low")
    b = compute_cache_key("abc123", "extract_v2", "v2", "gemini-3.5-flash-lite", "off")
    assert a != b


async def test_same_bytes_different_model_is_not_a_cache_hit(db, image_bytes):
    settings = Settings(cache_enabled=True)
    client = FakeClient([_call_result(VALID_RAW_JSON), _call_result(VALID_RAW_JSON)])

    first = await _run(settings, client, db, image_bytes, model_name="model-a")
    assert first.cache_hit is False

    second = await _run(settings, client, db, image_bytes, model_name="model-b")
    assert second.cache_hit is False
    assert client.calls == 2


async def test_same_bytes_different_reasoning_effort_is_not_a_cache_hit(db, image_bytes):
    settings = Settings(cache_enabled=True)
    client = FakeClient([_call_result(VALID_RAW_JSON), _call_result(VALID_RAW_JSON)])

    first = await _run(settings, client, db, image_bytes, reasoning_effort="low")
    assert first.cache_hit is False

    second = await _run(settings, client, db, image_bytes, reasoning_effort="off")
    assert second.cache_hit is False
    assert client.calls == 2


def test_cache_get_expires_past_ttl(db):
    from datetime import datetime, timedelta

    from app.db import cache_set

    cache_set(db, "some-key", {"foo": "bar"})
    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    db.execute(
        "UPDATE extraction_cache SET created_at = ? WHERE cache_key = ?", (stale, "some-key")
    )
    db.commit()

    assert cache_get(db, "some-key", ttl_days=1) is None
    assert cache_get(db, "some-key", ttl_days=0) == {"foo": "bar"}  # 0 = never expires
