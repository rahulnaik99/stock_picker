"""Shared agent plumbing: prompt loading, LLM call, JSON validation, result building.

Pattern for every specialist (see each agent.py):
    1. call its MCP tools (allow-listed)  -> evidence
    2. if there is no usable evidence     -> status "unavailable" (LLM is NOT asked to guess)
    3. LLM reads prompt.md + evidence     -> score / reasoning (JSON, validated, one retry)
    4. code applies hard rules (caps, overrides) and returns an AgentResult
This works with any model: no provider-specific tool-calling or structured-output features are needed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas import AgentResult, LLMAdjustOutput, LLMScoreOutput
from app.service.logger import get_logger, log_event, save_trace, timed
from app.service.models.llm_factory import get_llm
from app.service.models.telemetry import invoke_with_telemetry

log = get_logger(__name__)
MAX_EVIDENCE_CHARS = 14000
MAX_CONTEXT_STORED = 60000
MAX_ADJUST = 2.0   # in baseline mode the LLM may nudge the code-computed baseline by at most +/- this


def load_prompt(agent_file: str, **placeholders: str) -> str:
    """Read prompt.md that sits next to the agent's agent.py; fill {{placeholders}}."""
    text = (Path(agent_file).with_name("prompt.md")).read_text(encoding="utf-8")
    for k, v in placeholders.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply (handles ```json fences and chatter)."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start: i + 1])
    raise ValueError("unterminated JSON object")


# URLs are for humans (the UI/DB keep them); the LLM cannot click them and they are very long
# (Google-News redirect links run 500+ chars each), so strip them from the model payload to save tokens.
_LLM_NOISE_KEYS = ("url", "source_url", "link", "guid")


def compact(envelope: dict[str, Any], max_items: int = 15, drop_keys: tuple[str, ...] = _LLM_NOISE_KEYS) -> dict[str, Any]:
    """Trim a tool envelope for the prompt: cut long lists and drop noisy URL-ish fields (kept in storage)."""
    out = dict(envelope)
    data = out.get("data")
    if isinstance(data, list):
        if len(data) > max_items:
            out["truncated_from"] = len(data)
            data = data[:max_items]
        if drop_keys:
            data = [{k: v for k, v in item.items() if k not in drop_keys} if isinstance(item, dict) else item for item in data]
        out["data"] = data
    return out


def _serialize_evidence_payload(payload: dict[str, Any], max_chars: int) -> str:
    """Fit evidence under the character budget without ever cutting JSON mid-token/string."""
    def trim(value: Any, string_limit: int, list_limit: int) -> Any:
        if isinstance(value, dict):
            return {str(k): trim(v, string_limit, list_limit) for k, v in value.items()}
        if isinstance(value, list):
            return [trim(v, string_limit, list_limit) for v in value[:list_limit]]
        if isinstance(value, str) and len(value) > string_limit:
            return value[:string_limit] + " …[field truncated]"
        return value

    original_size = len(json.dumps(payload, default=str))
    if original_size <= max_chars:
        return json.dumps(payload, default=str)
    for string_limit, list_limit in ((4000, 15), (2000, 10), (1000, 8), (600, 5), (300, 3), (150, 2), (80, 1)):
        candidate = trim(payload, string_limit, list_limit)
        candidate["_evidence_truncation"] = {"original_chars": original_size, "budget_chars": max_chars}
        serialized = json.dumps(candidate, default=str)
        if len(serialized) <= max_chars:
            return serialized
    # Last-resort compact envelope preserves each evidence source's status and provenance.
    evidence = payload.get("evidence", {})
    summary = {}
    if isinstance(evidence, dict):
        for key, item in evidence.items():
            if isinstance(item, dict):
                summary[key] = {k: item.get(k) for k in ("status", "source", "retrieved_at", "message", "cache") if k in item}
            else:
                summary[key] = str(type(item).__name__)
    fallback = {"stock": payload.get("stock", {}), "evidence_summary": summary,
                "_evidence_truncation": {"original_chars": original_size, "budget_chars": max_chars, "mode": "provenance_only"}}
    serialized = json.dumps(fallback, default=str)
    if len(serialized) > max_chars:
        # Context metadata is small by contract; this still returns valid JSON if a caller violates it.
        serialized = json.dumps({"_evidence_truncation": {"original_chars": original_size, "budget_chars": max_chars}})
    return serialized


def source_rows(tool_results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"tool": k, "status": v.get("status"), "source": v.get("source"), "retrieved_at": v.get("retrieved_at"),
             "message": v.get("message"), "cache": v.get("cache")} for k, v in tool_results.items()]


def unavailable_result(agent: str, symbol: str, tool_results: dict[str, dict[str, Any]], why: str) -> AgentResult:
    return AgentResult(agent=agent, symbol=symbol, status="unavailable", reasoning=why, sources=source_rows(tool_results),
                       missing_data=[k for k, v in tool_results.items() if v.get("status") != "ok"])


async def analyze(*, agent: str, llm_key: str, symbol: str, system_prompt: str, evidence: dict[str, Any],
                  tool_results: dict[str, dict[str, Any]], context: dict[str, Any],
                  baseline: float | None = None, max_adjust: float = MAX_ADJUST) -> AgentResult:
    """Ask the agent's configured LLM to judge the evidence. Returns a validated AgentResult (never raises).

    Two modes:
    - baseline is None (legacy): the LLM returns an absolute `score` in [-10, 10].
    - baseline is a number: Python has already computed a deterministic baseline; the LLM returns a bounded
      `score_adjustment` and the final score = clamp(baseline + clamp(adjustment, +/-max_adjust))."""
    llm, spec = get_llm(llm_key)
    payload: dict[str, Any] = {"stock": context, "evidence": evidence}
    if baseline is not None:
        payload["baseline"] = {"score": round(baseline, 2), "max_adjustment": max_adjust,
                               "note": "Code computed this score from the evidence. Return score_adjustment in "
                                       f"[-{max_adjust}, +{max_adjust}]; the final score = baseline + your adjustment."}
    user = _serialize_evidence_payload(payload, MAX_EVIDENCE_CHARS * 2)
    messages = [("system", system_prompt), ("user", user)]
    schema = LLMAdjustOutput if baseline is not None else LLMScoreOutput
    raw, parsed, last_err = "", None, ""
    tok_in = tok_out = 0
    with timed() as t:
        for attempt in (1, 2):
            try:
                resp = await invoke_with_telemetry(llm, messages, agent=agent, provider=spec.provider, model=spec.model)
                ti, to = getattr(resp, "_ekx_tokens", (0, 0))
                tok_in, tok_out = tok_in + (ti or 0), tok_out + (to or 0)
                raw = resp.content if isinstance(resp.content, str) else json.dumps(resp.content)
                parsed = schema(**extract_json(raw))
                break
            except (ValueError, ValidationError, json.JSONDecodeError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log.warning("%s reply invalid (attempt %d): %s", agent, attempt, last_err[:200])
                messages = messages + [("assistant", raw or "(empty)"),
                                       ("user", "Your reply was not valid. Return ONLY the JSON object from the output spec.")]
            except Exception as exc:  # noqa: BLE001 - network/auth/etc from the provider
                last_err = f"{type(exc).__name__}: {exc}"
                log.error("%s LLM call failed (%s/%s): %s", agent, spec.provider, spec.model, last_err[:300])
                if any(w in last_err.lower() for w in ("context size", "context length", "length limit", "num_ctx")):
                    log.error("%s hit the model's context limit. Raise it on the Ollama SERVER (per-call num_ctx is ignored by "
                              "Ollama's /v1 endpoint): build a Modelfile with `PARAMETER num_ctx 16384` (see app/config/ollama/) "
                              "or start Ollama with OLLAMA_CONTEXT_LENGTH=16384.", agent)
                break
    save_trace(agent, {"provider": spec.provider, "model": spec.model, "system": system_prompt, "user": user, "reply": raw, "error": last_err})
    log_event("llm_call", agent=agent, provider=spec.provider, model=spec.model, ms=t["ms"], ok=parsed is not None, error=last_err[:200])
    base = dict(agent=agent, symbol=symbol, provider=spec.provider, model=spec.model, duration_ms=t["ms"], sources=source_rows(tool_results),
                input_tokens=tok_in, output_tokens=tok_out, context_used=user[:MAX_CONTEXT_STORED])
    if parsed is None:
        return AgentResult(**base, status="error", reasoning=f"LLM output unusable: {last_err[:300]}")
    note = ""
    if baseline is not None:
        # Prefer an explicit adjustment; tolerate a model that returned an absolute score by converting it.
        if parsed.score_adjustment is not None:
            raw_adj = float(parsed.score_adjustment)
        elif parsed.score is not None:
            raw_adj = float(parsed.score) - baseline
        else:
            raw_adj = 0.0
        adj = max(-max_adjust, min(max_adjust, raw_adj))
        if abs(raw_adj - adj) > 1e-9:
            note = f"adjustment {round(raw_adj, 2)} clamped to +/-{max_adjust}"
        score = max(-10.0, min(10.0, baseline + adj))
        extra = dict(parsed.extra or {})
        extra.update({"baseline_score": round(baseline, 2), "score_adjustment": round(adj, 2)})
        return AgentResult(**base, status="ok", score=round(score, 2), confidence=parsed.confidence, reasoning=parsed.reasoning,
                           evidence=parsed.evidence, missing_data=parsed.missing_data, extra=extra, note=note)
    score = parsed.score
    if not -10 <= score <= 10:
        note = f"score {score} clamped to [-10, 10]"
        score = max(-10.0, min(10.0, score))
    return AgentResult(**base, status="ok", score=round(score, 2), confidence=parsed.confidence, reasoning=parsed.reasoning,
                       evidence=parsed.evidence, missing_data=parsed.missing_data, extra=parsed.extra, note=note)
