import pytest

from app.service.models import llm_factory as f

CFG = {
    "default": {"provider": "openai", "model": "gpt-4o-mini"},
    "fundamental": {"provider": "openai", "model": "gpt-4o", "temperature": 0.3},
    "indicator": "gpt-4o-mini",
}


def test_per_agent_openai_model():
    spec = f.resolve_spec("fundamental", CFG)
    assert (spec.provider, spec.model, spec.temperature) == ("openai", "gpt-4o", 0.3)


def test_unknown_agent_uses_default():
    assert f.resolve_spec("something_new", CFG).model == "gpt-4o-mini"


def test_shorthand_uses_openai_provider(monkeypatch):
    from app.service.settings import get_settings

    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    spec = f.resolve_spec("indicator", CFG)
    assert (spec.provider, spec.model) == ("openai", "gpt-4o-mini")


def test_missing_default_raises():
    with pytest.raises(f.LLMConfigError):
        f.resolve_spec("x", {"fundamental": {"model": "m"}})


def test_ollama_provider_builds_chatopenai(monkeypatch):
    import sys
    import types
    from app.service.settings import get_settings

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setitem(sys.modules, "langchain_openai", types.SimpleNamespace(ChatOpenAI=FakeChatOpenAI))
    monkeypatch.setattr(get_settings(), "ollama_base_url", "http://localhost:11434")
    f._build.cache_clear()
    llm, spec = f.get_llm("a", {"a": {"provider": "ollama", "model": "qwen3:4b-instruct"}})
    # Ollama clients are wrapped in a per-model concurrency gate.
    assert type(llm._model).__name__ == "FakeChatOpenAI"
    assert llm._model.kwargs["model"] == "qwen3:4b-instruct"
    assert llm._model.kwargs["base_url"] == "http://localhost:11434/v1"
    assert llm._model.kwargs["api_key"] == "ollama"
    assert llm._model.kwargs["max_retries"] == 0
    assert spec.provider == "ollama"
    # Context window is raised so large agent prompts + their JSON answer fit (Ollama defaults to 4096).
    assert llm._model.kwargs["extra_body"] == {"options": {"num_ctx": get_settings().ollama_num_ctx}}
    f._build.cache_clear()


def test_openai_provider_builds_chatopenai(monkeypatch):
    import sys
    import types
    from app.service.settings import get_settings

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setitem(sys.modules, "langchain_openai", types.SimpleNamespace(ChatOpenAI=FakeChatOpenAI))
    monkeypatch.setattr(get_settings(), "openai_api_key", "test-key")
    f._build.cache_clear()
    llm, spec = f.get_llm("a", {"a": {"provider": "openai", "model": "gpt-4o"}})
    assert type(llm).__name__ == "FakeChatOpenAI"
    assert llm.kwargs["model"] == "gpt-4o"
    assert spec.provider == "openai"
    f._build.cache_clear()


def test_force_provider_overrides_json(monkeypatch):
    from app.service.settings import get_settings

    monkeypatch.setattr(get_settings(), "llm_force_provider", "stub")
    assert f.resolve_spec("fundamental", CFG).provider == "stub"


def test_shipped_model_config_defaults_to_local_ollama():
    cfg = f.load_model_config()
    for agent in ("fundamental", "industry", "indicator", "trend_flow", "option_chain", "decision_maker"):
        assert f.resolve_spec(agent, cfg).provider == "ollama"
        assert f.resolve_spec(agent, cfg).model == "gemma3-12b-16k"


def test_olmx_provider_builds_chatopenai_with_custom_base_url_and_key(monkeypatch):
    import sys
    import types
    from app.service.settings import get_settings

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setitem(sys.modules, "langchain_openai", types.SimpleNamespace(ChatOpenAI=FakeChatOpenAI))
    settings = get_settings()
    monkeypatch.setattr(settings, "olmx_base_url", "http://127.0.0.1:8008/v1")
    monkeypatch.setattr(settings, "olmx_api_key", "test-olmx-key")
    f._build.cache_clear()
    llm, spec = f.get_llm("a", {"a": {"provider": "olmx", "model": "test-model"}})
    model = llm._model
    assert model.kwargs["model"] == "test-model"
    assert model.kwargs["base_url"] == "http://127.0.0.1:8008/v1"
    assert model.kwargs["api_key"] == "test-olmx-key"
    assert spec.provider == "olmx"
    f._build.cache_clear()


def test_force_model_overrides_json(monkeypatch):
    from app.service.settings import get_settings

    monkeypatch.setattr(get_settings(), "llm_force_model", "olmx-test-model")
    spec = f.resolve_spec("fundamental", CFG)
    assert spec.model == "olmx-test-model"
