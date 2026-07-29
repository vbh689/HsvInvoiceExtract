import json

import pytest

from app.llm import (
    MalformedModelOutputError,
    MockVisionClient,
    OpenAICompatibleClient,
    _parse_usage,
    build_client,
)
from app.settings import ModelConfig, Settings


class _FakeUsage:
    def __init__(self, data: dict):
        self._data = data

    def model_dump(self) -> dict:
        return self._data


def _model(**overrides) -> ModelConfig:
    defaults = {"name": "test-model", "price_per_1m_input": 1.0, "price_per_1m_output": 2.0}
    defaults.update(overrides)
    return ModelConfig(**defaults)


def test_parse_usage_computed_when_no_provider_cost():
    model = _model(price_per_1m_input=1.0, price_per_1m_output=2.0)
    usage = _FakeUsage({"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500})

    tokens_in, tokens_out, tokens_total, tokens_cached, cost_usd, cost_source, raw = _parse_usage(
        usage, model
    )

    assert (tokens_in, tokens_out, tokens_total, tokens_cached) == (1000, 500, 1500, 0)
    assert cost_source == "computed"
    assert cost_usd == pytest.approx(1000 / 1_000_000 * 1.0 + 500 / 1_000_000 * 2.0)


def test_parse_usage_provider_cost_wins():
    model = _model(price_per_1m_input=1.0, price_per_1m_output=2.0)
    usage = _FakeUsage(
        {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500, "cost": 0.0042}
    )

    *_, cost_usd, cost_source, _raw = _parse_usage(usage, model)

    assert cost_source == "provider"
    assert cost_usd == pytest.approx(0.0042)


def test_parse_usage_reads_cached_tokens():
    model = _model()
    usage = _FakeUsage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
            "prompt_tokens_details": {"cached_tokens": 200},
        }
    )

    _, _, _, tokens_cached, *_ = _parse_usage(usage, model)
    assert tokens_cached == 200


def test_parse_usage_no_usage_object():
    model = _model()
    tokens_in, tokens_out, tokens_total, tokens_cached, cost_usd, cost_source, raw = _parse_usage(
        None, model
    )
    assert (tokens_in, tokens_out, tokens_total, tokens_cached) == (0, 0, 0, 0)
    assert cost_usd == 0.0
    assert cost_source == "computed"
    assert raw == {}


async def test_mock_client_returns_fixture_content():
    settings = Settings(mock_mode=True)
    client = build_client(settings, fixture_name="default")
    result = await client.extract(images=[b"fake"], prompt="dummy prompt")

    assert result.cost_source == "computed"
    assert result.tokens_in > 0 and result.tokens_out > 0
    assert len(result.raw_json["line_items"]) == 2


async def test_mock_client_missing_fixture_raises():
    settings = Settings(mock_mode=True)
    client = MockVisionClient(settings, "does-not-exist", _model())
    with pytest.raises(MalformedModelOutputError):
        await client.extract(images=[], prompt="p")


def test_model_override_uses_own_base_url_and_api_key():
    settings = Settings(llm_base_url="https://shared.example/v1", llm_api_key="shared-key")
    overridden = _model(base_url="https://own.example/v1", api_key="own-key")
    client = OpenAICompatibleClient(settings, overridden)

    assert str(client._client.base_url) == "https://own.example/v1/"
    assert client._client.api_key == "own-key"


def test_model_without_override_falls_back_to_shared_settings():
    settings = Settings(llm_base_url="https://shared.example/v1", llm_api_key="shared-key")
    client = OpenAICompatibleClient(settings, _model())

    assert str(client._client.base_url) == "https://shared.example/v1/"
    assert client._client.api_key == "shared-key"


class _StubCompletions:
    """Captures kwargs passed to chat.completions.create instead of hitting the network."""

    def __init__(self):
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        message = type(
            "Msg", (), {"content": json.dumps({"line_items": [], "model_notes": None})}
        )()
        choice = type("Choice", (), {"message": message})()
        response = type("Response", (), {"choices": [choice], "usage": None})()
        return response


async def test_extract_sends_reasoning_effort_low_by_default():
    settings = Settings(llm_reasoning_effort="low", llm_supports_structured_output=False)
    client = OpenAICompatibleClient(settings, _model())
    stub = _StubCompletions()
    client._client.chat.completions = stub

    await client.extract(images=[b"fake"], prompt="dummy prompt")

    assert stub.kwargs["reasoning_effort"] == "low"


async def test_extract_omits_reasoning_effort_when_off():
    settings = Settings(llm_reasoning_effort="off", llm_supports_structured_output=False)
    client = OpenAICompatibleClient(settings, _model())
    stub = _StubCompletions()
    client._client.chat.completions = stub

    await client.extract(images=[b"fake"], prompt="dummy prompt")

    assert "reasoning_effort" not in stub.kwargs
