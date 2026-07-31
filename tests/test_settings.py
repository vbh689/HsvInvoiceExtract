import pytest

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


def test_env_file_none_ignores_discoverable_dotenv(monkeypatch, tmp_path):
    """The class-level `env_file=".env"` is CWD-relative, so `_env_file=None`
    must suppress it. Without this every test using `_settings()` silently
    picks up the developer's own slots -- and CI, which has no `.env`, never
    notices.
    """
    (tmp_path / ".env").write_text(
        "LLM_MODEL_1=from-cwd-dotenv\nLLM_MODEL_1_PRICE_IN=9\nLLM_MODEL_1_PRICE_OUT=9\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert [model.name for model in Settings(_env_file=None).llm_models] == [
        "gemini-3.5-flash-lite"
    ]
    # Same CWD, default env_file: the slot *is* picked up, proving the .env
    # above is discoverable and the assertion isn't vacuous.
    assert [model.name for model in Settings().llm_models] == ["from-cwd-dotenv"]


def test_custom_env_file_is_used_for_numbered_slots(tmp_path):
    env_file = tmp_path / "models.env"
    env_file.write_text(
        "\n".join(
            [
                "LLM_MODEL_1=from-custom-file",
                "LLM_MODEL_1_PRICE_IN=1.25",
                "LLM_MODEL_1_PRICE_OUT=2.5",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert [model.name for model in settings.llm_models] == ["from-custom-file"]
    assert settings.llm_models[0].price_per_1m_input == 1.25
    assert settings.llm_models[0].price_per_1m_output == 2.5


def test_environment_model_slot_overrides_custom_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "models.env"
    env_file.write_text(
        "\n".join(
            [
                "LLM_MODEL_1=from-custom-file",
                "LLM_MODEL_1_PRICE_IN=1.25",
                "LLM_MODEL_1_PRICE_OUT=2.5",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_MODEL_1", "from-environment")
    monkeypatch.setenv("LLM_MODEL_1_PRICE_IN", "3.5")
    monkeypatch.setenv("LLM_MODEL_1_PRICE_OUT", "4.5")

    settings = Settings(_env_file=env_file)

    assert [model.name for model in settings.llm_models] == ["from-environment"]
    assert settings.llm_models[0].price_per_1m_input == 3.5
    assert settings.llm_models[0].price_per_1m_output == 4.5


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


def test_per_model_reasoning_effort_override(monkeypatch):
    settings = _settings(
        monkeypatch,
        LLM_MODEL_1="first",
        LLM_MODEL_1_PRICE_IN="0.1",
        LLM_MODEL_1_PRICE_OUT="0.1",
        LLM_MODEL_2="second",
        LLM_MODEL_2_PRICE_IN="0.2",
        LLM_MODEL_2_PRICE_OUT="0.2",
        LLM_MODEL_2_REASONING_EFFORT="off",
    )
    first, second = settings.llm_models
    assert first.reasoning_effort is None
    assert second.reasoning_effort == "off"


def test_effective_reasoning_effort_defaults_to_shared_setting():
    settings = Settings(llm_reasoning_effort="low", _env_file=None)
    model = ModelConfig(name="m", price_per_1m_input=0.1, price_per_1m_output=0.1)
    assert settings.effective_reasoning_effort(model) == "low"


def test_effective_reasoning_effort_per_slot_override_wins():
    settings = Settings(llm_reasoning_effort="low", _env_file=None)
    model = ModelConfig(
        name="m", price_per_1m_input=0.1, price_per_1m_output=0.1, reasoning_effort="high"
    )
    assert settings.effective_reasoning_effort(model) == "high"


@pytest.mark.parametrize("value", ["off", "", "  OFF  "])
def test_effective_reasoning_effort_off_and_empty_mean_omit(value):
    settings = Settings(llm_reasoning_effort="low", _env_file=None)
    model = ModelConfig(
        name="m", price_per_1m_input=0.1, price_per_1m_output=0.1, reasoning_effort=value
    )
    assert settings.effective_reasoning_effort(model) is None


def test_effective_reasoning_effort_none_literal_passes_through():
    settings = Settings(llm_reasoning_effort="low", _env_file=None)
    model = ModelConfig(
        name="m", price_per_1m_input=0.1, price_per_1m_output=0.1, reasoning_effort="none"
    )
    assert settings.effective_reasoning_effort(model) == "none"
