import pytest
from pydantic import ValidationError

from app.schemas import RawExtraction, UsageInfo


def _complete_payload() -> dict:
    return {
        "supplier_name": None,
        "supplier_tax_code": None,
        "invoice_number": None,
        "invoice_date": None,
        "currency": None,
        "printed_subtotal": None,
        "printed_tax_amount": None,
        "printed_tax_rate": None,
        "printed_grand_total": None,
        "printed_discount": None,
        "stated_price_basis": "unknown",
        "line_items": [],
        "model_notes": None,
    }


def test_raw_extraction_requires_every_model_facing_key():
    payload = _complete_payload()
    payload.pop("printed_tax_rate")

    with pytest.raises(ValidationError):
        RawExtraction.model_validate(payload)


def test_raw_extraction_forbids_unknown_keys():
    payload = _complete_payload()
    payload["tax_rate"] = "10%"

    with pytest.raises(ValidationError):
        RawExtraction.model_validate(payload)


def test_model_json_schema_is_closed_and_required():
    schema = RawExtraction.model_json_schema()
    model_keys = set(schema["properties"]) - {"schema_version"}

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == model_keys
    line_schema = schema["$defs"]["RawLineItem"]
    assert line_schema["additionalProperties"] is False
    assert set(line_schema["required"]) == set(line_schema["properties"])


def test_usage_info_defaults_cost_vnd_for_pre_existing_cached_json():
    """cost_vnd was added after cost_usd; cached/logged rows written before
    that change won't have it in their stored JSON, so parsing must not
    break -- it should just default to 0 rather than raising.
    """
    payload = {
        "cached": True,
        "model": "fake-model",
        "attempts": 1,
        "latency_ms": 10.0,
        "tokens_in": 1,
        "tokens_out": 1,
        "tokens_total": 2,
        "cost_usd": 0.001,
        "prompt_version": "v1",
        "schema_version": "v1",
    }
    usage = UsageInfo.model_validate(payload)
    assert usage.cost_vnd == 0
