"""Decision Maker: reads every specialist's score AND reasoning, explains conflicts, proposes direction/status.

It does NOT own the final score: scoring/policy.py computes that deterministically. The LLM's proposed score is
compared against it and any gap is recorded.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.Agents.base import extract_json, load_prompt
from app.schemas import AgentResult, DecisionOutput
from app.service.logger import get_logger, log_event, save_trace, timed
from app.service.models.llm_factory import get_llm
from app.service.models.telemetry import invoke_with_telemetry

NAME = "decision_maker"
log = get_logger(__name__)


def _brief(r: AgentResult) -> dict[str, Any]:
    return {"status": r.status, "score": r.score, "confidence": r.confidence, "reasoning": r.reasoning, "evidence": r.evidence[:6],
            "missing_data": r.missing_data, "extra": r.extra, "note": r.note,
            "sources": [{"tool": s["tool"], "status": s["status"], "retrieved_at": s["retrieved_at"]} for s in r.sources]}


async def run(symbol: str, meta: dict[str, Any], results: dict[str, AgentResult], policy: dict[str, Any], as_of: str | None) -> tuple[DecisionOutput | None, str, str]:
    """Returns (decision or None on failure, provider, model)."""
    llm, spec = get_llm(NAME)
    payload = {"stock": {**meta, "as_of": as_of}, "specialists": {k: _brief(v) for k, v in results.items()}, "policy": policy}
    messages = [("system", load_prompt(__file__)), ("user", json.dumps(payload, default=str))]
    raw, decision, err = "", None, ""
    with timed() as t:
        for attempt in (1, 2):
            try:
                resp = await invoke_with_telemetry(llm, messages, agent=NAME, provider=spec.provider, model=spec.model)
                raw = resp.content if isinstance(resp.content, str) else json.dumps(resp.content)
                decision = DecisionOutput(**extract_json(raw))
                break
            except (ValueError, ValidationError, json.JSONDecodeError) as exc:
                err = f"{type(exc).__name__}: {exc}"
                messages = messages + [("assistant", raw or "(empty)"), ("user", "Invalid reply. Return ONLY the JSON object from the output spec.")]
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                log.error("decision_maker LLM call failed (%s/%s): %s", spec.provider, spec.model, err[:300])
                break
    save_trace(NAME, {"provider": spec.provider, "model": spec.model, "system": messages[0][1], "user": messages[1][1], "reply": raw, "error": err})
    log_event("llm_call", agent=NAME, provider=spec.provider, model=spec.model, ms=t["ms"], ok=decision is not None, error=err[:200])
    return decision, spec.provider, spec.model
