"""Compatibility wrapper for the canonical OpenAI-only LLM factory.

The active implementation lives in app.service.models.llm_factory. All production inference
uses OpenAI; the "stub" provider is only for offline tests and demos.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.service.models import llm_factory as _factory


class LLMConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    temperature: float | None = 0.1
    max_tokens: int | None = 2000

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def load_model_config():
    return _factory.load_model_config()


def resolve(agent: str, config: dict[str, Any] | None = None) -> ModelSpec:
    spec = _factory.resolve_spec(agent, config)
    return ModelSpec(spec.provider, spec.model, spec.temperature, spec.max_tokens)


def build_chat_model(spec: ModelSpec, agent: str = "") -> Any:
    # Re-resolve through the canonical factory so provider restrictions remain consistent.
    model, _ = _factory.get_llm(agent or "default", {
        agent or "default": {
            "provider": spec.provider, "model": spec.model,
            "temperature": spec.temperature, "max_tokens": spec.max_tokens,
        }
    })
    return model


def get_llm(agent: str) -> Any:
    model, _ = _factory.get_llm(agent)
    return model


def describe(agent: str) -> str:
    return _factory.resolve_spec(agent).provider + ":" + _factory.resolve_spec(agent).model


def validate_all(agents: list[str]) -> list[str]:
    problems = []
    for agent in agents:
        try:
            _factory.get_llm(agent)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{agent}: {exc}")
    return problems


def clear_cache() -> None:
    _factory._build.cache_clear()
