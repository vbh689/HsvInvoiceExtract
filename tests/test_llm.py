import pytest

from app.llm import MalformedModelOutputError, MockVisionClient, _parse_usage, build_client
from app.settings import Settings


class _FakeUsage:
    def __init__(self, data: dict):
        self._data = data

    def model_dump(self) -> dict:
        return self._data


def test_parse_usage_computed_when_no_provider_cost():
    settings = Settings(llm_price_per_1m_input=1.0, llm_price_per_1m_output=2.0)
    usage = _FakeUsage({"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500})

    tokens_in, tokens_out, tokens_total, tokens_cached, cost_usd, cost_source, raw = _parse_usage(
        usage, settings
    )

    assert (tokens_in, tokens_out, tokens_total, tokens_cached) == (1000, 500, 1500, 0)
    assert cost_source == "computed"
    assert cost_usd == pytest.approx(1000 / 1_000_000 * 1.0 + 500 / 1_000_000 * 2.0)


def test_parse_usage_provider_cost_wins():
    settings = Settings(llm_price_per_1m_input=1.0, llm_price_per_1m_output=2.0)
    usage = _FakeUsage(
        {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500, "cost": 0.0042}
    )

    *_, cost_usd, cost_source, _raw = _parse_usage(usage, settings)

    assert cost_source == "provider"
    assert cost_usd == pytest.approx(0.0042)


def test_parse_usage_reads_cached_tokens():
    settings = Settings()
    usage = _FakeUsage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
            "prompt_tokens_details": {"cached_tokens": 200},
        }
    )

    _, _, _, tokens_cached, *_ = _parse_usage(usage, settings)
    assert tokens_cached == 200


def test_parse_usage_no_usage_object():
    settings = Settings()
    tokens_in, tokens_out, tokens_total, tokens_cached, cost_usd, cost_source, raw = _parse_usage(
        None, settings
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
    client = MockVisionClient(settings, "does-not-exist")
    with pytest.raises(MalformedModelOutputError):
        await client.extract(images=[], prompt="p")
