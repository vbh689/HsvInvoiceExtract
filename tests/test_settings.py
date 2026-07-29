from app.settings import ModelConfig, Settings


def _settings(monkeypatch, **env) -> Settings:
    """Builds a Settings() instance isolated from the real repo .env file --
    only the env vars explicitly passed here are visible to the numbered
    model-slot source.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_no_model_slots_falls_back_to_single_default(monkeypatch):
    settings = _settings(monkeypatch)
    assert len(settings.llm_models) == 1
    assert settings.llm_models[0].name == "gemini-3.5-flash-lite"
    assert settings.default_model.name == "gemini-3.5-flash-lite"


def test_numbered_slots_are_parsed_in_order(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_MODEL_1="gemini-a",
        LLM_MODEL_1_PRICE_IN="0.1",
        LLM_MODEL_1_PRICE_OUT="0.2",
        LLM_MODEL_2="gpt-b",
        LLM_MODEL_2_PRICE_IN="0.3",
        LLM_MODEL_2_PRICE_OUT="0.4",
    )
    names = [m.name for m in settings.llm_models]
    assert names == ["gemini-a", "gpt-b"]
    assert settings.llm_models[0].price_per_1m_input == 0.1
    assert settings.llm_models[1].price_per_1m_output == 0.4


def test_slot_order_follows_index_not_declaration_order(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_MODEL_2="second",
        LLM_MODEL_2_PRICE_IN="0.2",
        LLM_MODEL_2_PRICE_OUT="0.2",
        LLM_MODEL_1="first",
        LLM_MODEL_1_PRICE_IN="0.1",
        LLM_MODEL_1_PRICE_OUT="0.1",
    )
    names = [m.name for m in settings.llm_models]
    assert names == ["first", "second"]


def test_per_model_base_url_and_api_key_override(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_MODEL_1="gemini-a",
        LLM_MODEL_1_PRICE_IN="0.1",
        LLM_MODEL_1_PRICE_OUT="0.2",
        LLM_MODEL_2="gpt-b",
        LLM_MODEL_2_PRICE_IN="0.3",
        LLM_MODEL_2_PRICE_OUT="0.4",
        LLM_MODEL_2_BASE_URL="https://api.openai.com/v1",
        LLM_MODEL_2_API_KEY="sk-test",
    )
    first, second = settings.llm_models
    assert first.base_url is None
    assert first.api_key is None
    assert second.base_url == "https://api.openai.com/v1"
    assert second.api_key == "sk-test"


def test_first_slot_is_default(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_MODEL_1="first",
        LLM_MODEL_1_PRICE_IN="0.1",
        LLM_MODEL_1_PRICE_OUT="0.1",
        LLM_MODEL_2="second",
        LLM_MODEL_2_PRICE_IN="0.2",
        LLM_MODEL_2_PRICE_OUT="0.2",
    )
    assert settings.default_model.name == "first"


def test_resolve_model_none_returns_default(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_MODEL_1="first",
        LLM_MODEL_1_PRICE_IN="0.1",
        LLM_MODEL_1_PRICE_OUT="0.1",
    )
    assert settings.resolve_model(None) is settings.default_model


def test_resolve_model_nonexistent_silently_returns_default(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_MODEL_1="first",
        LLM_MODEL_1_PRICE_IN="0.1",
        LLM_MODEL_1_PRICE_OUT="0.1",
        LLM_MODEL_2="second",
        LLM_MODEL_2_PRICE_IN="0.2",
        LLM_MODEL_2_PRICE_OUT="0.2",
    )
    resolved = settings.resolve_model("nonexistent-model")
    assert resolved.name == "first"


def test_resolve_model_matches_by_name(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_MODEL_1="first",
        LLM_MODEL_1_PRICE_IN="0.1",
        LLM_MODEL_1_PRICE_OUT="0.1",
        LLM_MODEL_2="second",
        LLM_MODEL_2_PRICE_IN="0.2",
        LLM_MODEL_2_PRICE_OUT="0.2",
    )
    resolved = settings.resolve_model("second")
    assert resolved.name == "second"


def test_init_kwarg_wins_over_numbered_slots(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_1", "from-env")
    monkeypatch.setenv("LLM_MODEL_1_PRICE_IN", "0.1")
    monkeypatch.setenv("LLM_MODEL_1_PRICE_OUT", "0.1")
    settings = Settings(
        llm_models=[ModelConfig(name="from-kwarg", price_per_1m_input=9, price_per_1m_output=9)],
        _env_file=None,
    )
    assert settings.llm_models[0].name == "from-kwarg"
