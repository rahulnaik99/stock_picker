"""Provider-usage extraction and consistent per-invocation LLM telemetry."""
from __future__ import annotations

import json
import time
from typing import Any

from app.service import progress
from app.service.logger import log_event

def _message_text(messages: list[Any]) -> str:
    parts = []
    for message in messages:
        if isinstance(message, (tuple, list)) and len(message) >= 2:
            content = message[1]
        else:
            content = getattr(message, "content", message)
        parts.append(content if isinstance(content, str) else json.dumps(content, default=str))
    return "\n".join(parts)

def _provider_usage(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        inp = usage.get("input_tokens", usage.get("prompt_tokens"))
        out = usage.get("output_tokens", usage.get("completion_tokens"))
        if inp is not None or out is not None:
            return (int(inp) if inp is not None else None, int(out) if out is not None else None)
    meta = getattr(response, "response_metadata", None) or {}
    if isinstance(meta, dict):
        usage = meta.get("token_usage") or meta.get("usage") or {}
        if isinstance(usage, dict):
            inp = usage.get("prompt_tokens", usage.get("input_tokens"))
            out = usage.get("completion_tokens", usage.get("output_tokens"))
            if inp is not None or out is not None:
                return (int(inp) if inp is not None else None, int(out) if out is not None else None)
    return None, None

def _output_text(response: Any) -> str:
    content = getattr(response, "content", "")
    return content if isinstance(content, str) else json.dumps(content, default=str)

async def invoke_with_telemetry(llm: Any, messages: list[Any], *, agent: str, provider: str, model: str):
    """Invoke once and record provider token usage, or explicitly labelled character-based estimates."""
    input_text = _message_text(messages)
    started = time.perf_counter()
    status, error = "ok", ""
    response = None
    try:
        response = await llm.ainvoke(messages)
    except Exception as exc:
        status, error = "error", f"{type(exc).__name__}: {exc}"[:240]
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        output_text = _output_text(response) if response is not None else ""
        provider_input, provider_output = _provider_usage(response) if response is not None else (None, None)
        exact = provider_input is not None and provider_output is not None
        input_tokens = provider_input if provider_input is not None else (max(1, round(len(input_text) / 4)) if input_text else 0)
        output_tokens = provider_output if provider_output is not None else (max(1, round(len(output_text) / 4)) if output_text else 0)
        source = "provider_usage" if exact else ("mixed_provider_usage_estimate" if provider_input is not None or provider_output is not None else "estimated_chars_div_4")
        response_meta = getattr(response, "response_metadata", None) or {}
        queue_ms = float(response_meta.get("_local_queue_ms", 0) or 0) if isinstance(response_meta, dict) else 0.0
        inference_ms = float(response_meta.get("_local_inference_ms", duration_ms) or duration_ms) if isinstance(response_meta, dict) else duration_ms
        seconds = inference_ms / 1000
        log_event("llm_attempt", agent=agent, provider=provider, model=model, status=status,
                  duration_ms=duration_ms, queue_ms=round(queue_ms, 1), inference_ms=round(inference_ms, 1),
                  input_chars=len(input_text), output_chars=len(output_text),
                  input_tokens=input_tokens, output_tokens=output_tokens,
                  tokens_per_second=round(output_tokens / seconds, 2) if seconds else 0.0,
                  token_count_source=source, error=error)
        # Feed the live progress tracker (keyed by the current run/symbol context) so the UI can show tokens live.
        try:
            progress.record_tokens(agent, input_tokens, output_tokens, provider=provider, model=model, duration_ms=duration_ms)
        except Exception:  # noqa: BLE001 - telemetry must never break an LLM call
            pass
        # Expose this attempt's usage on the response so the caller can persist per-agent token totals.
        if response is not None:
            try:
                response._ekx_tokens = (input_tokens, output_tokens)
            except Exception:  # noqa: BLE001 - some response objects are immutable
                pass
    return response
