"""One function: get_llm(agent_name). Provider + model come from model_config.json.

OpenAI, OpenRouter, Ollama, MLX-LM, NVIDIA NIM, Gemini and any custom server all speak the
OpenAI chat protocol, so a single ChatOpenAI client with a different base_url covers them.
Anthropic uses its own client. "stub" is an offline fake used for dry runs and tests.

Add a provider = add one entry to PROVIDERS below.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from weakref import WeakKeyDictionary

from app.service.logger import get_logger
from app.service.settings import Settings, get_settings

log = get_logger(__name__)
_ollama_gates: WeakKeyDictionary[Any, dict[tuple[str, str], asyncio.Semaphore]] = WeakKeyDictionary()
CONFIG_PATH = Path(__file__).with_name("model_config.json")


class LLMConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    agent: str
    provider: str
    model: str
    temperature: float
    max_tokens: int


def load_model_config(path: Path | None = None) -> dict[str, Any]:
    # explicit path arg > run-level override in settings > default config file
    effective = path or (Path(get_settings().llm_config_path) if get_settings().llm_config_path else None) or CONFIG_PATH
    return json.loads(Path(effective).read_text(encoding="utf-8"))


def resolve_spec(agent: str, config: dict[str, Any] | None = None, settings: Settings | None = None) -> ModelSpec:
    s = settings or get_settings()
    cfg = config if config is not None else load_model_config()
    entry = cfg.get(agent) or cfg.get("default")
    if entry is None:
        raise LLMConfigError(f"No model configured for agent '{agent}' and no 'default' entry in model_config.json")
    if isinstance(entry, str):  # shorthand: "agent": "gpt-4o" -> provider from .env
        entry = {"model": entry}
    model = s.llm_force_model or entry.get("model")
    if not model:
        raise LLMConfigError(f"model_config.json entry for '{agent}' has no 'model'")
    return ModelSpec(
        agent=agent,
        provider=str(s.llm_force_provider or entry.get("provider") or s.llm_provider).lower(),
        model=model,
        temperature=float(entry.get("temperature", s.llm_temperature)),
        max_tokens=int(entry.get("max_tokens", s.llm_max_tokens)),
    )


# --------------------------------------------------------------------------- builders
def _openai_compatible(base_url: str | Callable[[Settings], str], key: Callable[[Settings], str]):
    def build(spec: ModelSpec, s: Settings):
        from langchain_openai import ChatOpenAI

        url = base_url(s) if callable(base_url) else base_url
        api_key = key(s) or "not-needed"  # local servers accept any key
        kwargs: dict[str, Any] = dict(
            model=spec.model, temperature=spec.temperature, max_tokens=spec.max_tokens,
            api_key=api_key, timeout=s.llm_timeout_seconds,
            max_retries=(s.ollama_max_retries if spec.provider == "ollama" else s.llm_max_retries),
        )
        if url:
            kwargs["base_url"] = url
        # Local OpenAI-compatible servers (Ollama/OLMX) honour response_format and return a clean JSON
        # object, which removes most of the validation re-asks small models would otherwise trigger.
        # The OpenAI API itself needs the word "json" in the prompt, so this stays scoped to local providers.
        if s.llm_json_object and spec.provider in ("ollama", "olmx"):
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
        # `options.num_ctx` is OLLAMA-SPECIFIC request syntax. Sending it to an MLX / OLMX server (which does
        # not understand it) can make that server return a malformed body with `choices: null`, which then
        # blows up inside langchain-openai as `'NoneType' object is not iterable`. So scope it to Ollama ONLY.
        # MLX-LM sizes its context from the loaded model and needs no per-request context hint.
        if spec.provider == "ollama" and s.ollama_num_ctx > 0:
            kwargs["extra_body"] = {"options": {"num_ctx": s.ollama_num_ctx}}
        return ChatOpenAI(**kwargs)

    return build


def _anthropic(spec: ModelSpec, s: Settings):
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=spec.model, temperature=spec.temperature, max_tokens=spec.max_tokens,
        api_key=s.anthropic_api_key, timeout=s.llm_timeout_seconds, max_retries=2,
    )


class _Msg:
    def __init__(self, content: str):
        self.content = content


class StubLLM:
    """Offline model for dry runs/tests. Returns deterministic JSON, one shape per agent."""

    def __init__(self, spec: ModelSpec):
        self.spec = spec

    async def ainvoke(self, messages: list[Any], **_: Any) -> _Msg:
        text = " ".join(str(getattr(m, "content", m)) for m in messages)
        h = int(hashlib.sha256((self.spec.agent + text[:400]).encode()).hexdigest(), 16)
        score = (h % 13) - 6  # -6..+6
        if self.spec.agent == "decision_maker":
            body = {
                "direction_view": "BULLISH" if score > 1 else "BEARISH" if score < -1 else "MIXED",
                "proposed_score": float(score), "trade_status": "POTENTIAL_ENTRY" if score > 3 else "WAIT_FOR_SETUP",
                "confidence": 0.5, "reasoning": "stub decision", "agreements": [], "conflicts": [],
                "risks": ["stub risk"], "next_catalyst": None, "invalidation": "stub",
            }
        else:
            body = {
                "score_adjustment": 0.0, "score": float(score), "confidence": 0.5, "reasoning": f"stub reasoning for {self.spec.agent}",
                "evidence": [], "missing_data": [], "extra": {},
            }
        return _Msg(json.dumps(body))


PROVIDERS: dict[str, Callable[[ModelSpec, Settings], Any]] = {
    # All production agents use the official OpenAI API. Stub is reserved for offline tests.
    "openai": _openai_compatible("", lambda s: s.openai_api_key),
    # Ollama exposes an OpenAI-compatible chat-completions endpoint at /v1.
    "ollama": _openai_compatible(
        # Accept either http://localhost:11434 or http://localhost:11434/v1.
        # ChatOpenAI appends /chat/completions, so /v1 must appear exactly once.
        lambda s: (
            s.ollama_base_url.rstrip("/")
            if s.ollama_base_url.rstrip("/").endswith("/v1")
            else s.ollama_base_url.rstrip("/") + "/v1"
        ),
        lambda s: "ollama",
    ),
    # OLMX exposes an OpenAI-compatible API (e.g. http://127.0.0.1:8008/v1).
    "olmx": _openai_compatible(
        lambda s: (
            s.olmx_base_url.rstrip("/")
            if s.olmx_base_url.rstrip("/").endswith("/v1")
            else s.olmx_base_url.rstrip("/") + "/v1"
        ),
        lambda s: s.olmx_api_key,
    ),
    "stub": lambda spec, s: StubLLM(spec),
}


class _SerializedOllamaModel:
    """Limit simultaneous inference per model; local RAM/CPU-bound servers degrade under fan-out."""
    def __init__(self, model: Any, spec: ModelSpec):
        self._model = model
        self._spec = spec

    async def ainvoke(self, messages: list[Any], **kwargs: Any):
        loop = asyncio.get_running_loop()
        gates = _ollama_gates.setdefault(loop, {})
        key = (self._spec.provider, self._spec.model)
        gate = gates.setdefault(key, asyncio.Semaphore(max(1, get_settings().ollama_max_concurrent_requests)))
        queued_at = asyncio.get_running_loop().time()
        async with gate:
            started = asyncio.get_running_loop().time()
            queue_ms = round((started - queued_at) * 1000, 1)
            response = await self._model.ainvoke(messages, **kwargs)
            inference_ms = round((asyncio.get_running_loop().time() - started) * 1000, 1)
            # Keep queue wait separate from actual model generation for trustworthy tokens/sec.
            metadata = getattr(response, "response_metadata", None)
            if isinstance(metadata, dict):
                metadata["_local_queue_ms"] = queue_ms
                metadata["_local_inference_ms"] = inference_ms
            return response


@lru_cache(maxsize=64)
def _build(spec: ModelSpec):
    builder = PROVIDERS.get(spec.provider)
    if builder is None:
        raise LLMConfigError(
            f"Unsupported provider '{spec.provider}'. Known providers: {sorted(PROVIDERS)}"
        )
    model = builder(spec, get_settings())
    return _SerializedOllamaModel(model, spec) if spec.provider in {"ollama", "olmx"} else model


def get_llm(agent: str, config: dict[str, Any] | None = None):
    """Return a chat model for this agent (anything with `await ainvoke(messages)`)."""
    spec = resolve_spec(agent, config)
    log.debug("LLM for %s -> provider=%s model=%s", agent, spec.provider, spec.model)
    return _build(spec), spec
