from types import SimpleNamespace

from app.service.models.telemetry import _provider_usage


def test_provider_usage_reads_langchain_usage_metadata():
    response = SimpleNamespace(usage_metadata={"input_tokens": 123, "output_tokens": 45})
    assert _provider_usage(response) == (123, 45)


def test_provider_usage_reads_openai_response_metadata():
    response = SimpleNamespace(usage_metadata=None, response_metadata={"token_usage": {"prompt_tokens": 80, "completion_tokens": 20}})
    assert _provider_usage(response) == (80, 20)


def test_provider_usage_returns_none_when_missing():
    response = SimpleNamespace(usage_metadata=None, response_metadata={})
    assert _provider_usage(response) == (None, None)
