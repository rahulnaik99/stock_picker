"""Deterministic scoring policy. Weights, direction gate, data-quality gate, trade-status validation, ranking.

Nothing here calls an LLM. Missing/unavailable inputs are never replaced by zero:
the weighted score is computed over the available inputs (weights re-normalised) and `data_completeness`
reports how much of the total weight was actually present.
"""
from __future__ import annotations

import json
from typing import Any

from app.schemas import AGENT_NAMES, AgentResult, DecisionOutput, StockResult
from app.service.settings import ROOT

WEIGHTS_PATH = ROOT / "app" / "config" / "weights.json"


def load_config() -> dict[str, Any]:
    cfg = json.loads(WEIGHTS_PATH.read_text())
    w = cfg["weights"]
    if set(w) != set(AGENT_NAMES):
        raise ValueError(f"weights.json must define exactly {AGENT_NAMES}")
    if abs(sum(w.values()) - 1.0) > 1e-6:
        raise ValueError(f"weights must sum to 1.0, got {sum(w.values())}")
    return cfg


def usable(r: AgentResult | None) -> bool:
    return r is not None and r.status == "ok" and r.score is not None and -10 <= r.score <= 10


def compute_policy(results: dict[str, AgentResult], cfg: dict[str, Any]) -> dict[str, Any]:
    w = cfg["weights"]
    avail = {k: results[k] for k in AGENT_NAMES if usable(results.get(k))}
    wsum = sum(w[k] for k in avail)
    completeness = round(wsum, 4)  # weights sum to 1, so this is the share of evidence present
    score = round(sum(w[k] * avail[k].score for k in avail) / wsum, 2) if wsum else None
    missing_required = [k for k in cfg["required"] if k not in avail]

    if missing_required or score is None:
        consensus, data_status = "INSUFFICIENT_DATA", "INSUFFICIENT"
    else:
        vals = [r.score for r in avail.values()]
        pos, neg, zero = any(v > 0 for v in vals), any(v < 0 for v in vals), any(v == 0 for v in vals)
        consensus = "MIXED" if pos and neg else "NEUTRAL_OR_MIXED" if zero else "BULLISH_CONSENSUS" if pos else "BEARISH_CONSENSUS"
        data_status = "COMPLETE" if len(avail) == len(AGENT_NAMES) else "PARTIAL"
        if completeness < cfg["min_data_completeness"]:
            data_status = "INSUFFICIENT"
            consensus = "INSUFFICIENT_DATA"
    ind = results.get("indicator")
    return {
        "policy_score": score, "weights": w, "weights_version": cfg["version"], "consensus_status": consensus,
        "data_status": data_status, "data_completeness": completeness, "missing_required": missing_required,
        "unavailable": [k for k in AGENT_NAMES if k not in avail],
        "indicator_extended": bool(ind and ind.extra.get("extended")),
    }


_DIRECTION = {"BULLISH_CONSENSUS": "BULLISH", "BEARISH_CONSENSUS": "BEARISH", "MIXED": "MIXED",
              "NEUTRAL_OR_MIXED": "MIXED", "INSUFFICIENT_DATA": "INSUFFICIENT_DATA"}


def validate_trade_status(requested: str | None, policy: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[str]]:
    """The LLM may suggest a status; code decides what is allowed."""
    notes: list[str] = []
    allowed = {"POTENTIAL_ENTRY", "WAIT_FOR_SETUP", "AVOID", "INSUFFICIENT_DATA"}
    status = requested if requested in allowed else "WAIT_FOR_SETUP"
    if requested not in allowed:
        notes.append(f"unknown status '{requested}' replaced by WAIT_FOR_SETUP")
    score, cons = policy["policy_score"], policy["consensus_status"]
    if cons == "INSUFFICIENT_DATA":
        return "INSUFFICIENT_DATA", notes + ([f"{requested} -> INSUFFICIENT_DATA (data gate)"] if requested != "INSUFFICIENT_DATA" else [])
    if cons == "BEARISH_CONSENSUS" and status != "AVOID":
        notes.append(f"{status} -> AVOID (bearish consensus)")
        status = "AVOID"
    if status == "POTENTIAL_ENTRY":
        why = []
        if cons != "BULLISH_CONSENSUS":
            why.append("no bullish consensus")
        if policy["data_status"] != "COMPLETE" and policy["data_completeness"] < cfg["min_data_completeness"]:
            why.append("data completeness below minimum")
        if score is None or score < cfg["min_final_score_for_entry"]:
            why.append("score below entry minimum")
        if cfg["overextended_blocks_entry"] and policy["indicator_extended"]:
            why.append("price extended")
        if why:
            notes.append("POTENTIAL_ENTRY -> WAIT_FOR_SETUP (" + ", ".join(why) + ")")
            status = "WAIT_FOR_SETUP"
    if status == "INSUFFICIENT_DATA":  # LLM said so but our gate says data is fine
        notes.append("INSUFFICIENT_DATA -> WAIT_FOR_SETUP (data gate passed)")
        status = "WAIT_FOR_SETUP"
    return status, notes


def finalize(*, run_id: str, symbol: str, meta: dict[str, Any], as_of: str, results: dict[str, AgentResult],
             decision: DecisionOutput | None, cfg: dict[str, Any], error: str | None = None) -> StockResult:
    policy = compute_policy(results, cfg)
    score = policy["policy_score"]
    status, notes = validate_trade_status(decision.trade_status if decision else None, policy, cfg)
    valid = score is not None and policy["consensus_status"] != "INSUFFICIENT_DATA" and error is None
    if decision is None and valid:
        notes.append("decision maker unavailable; status set by policy only")
        status = "WAIT_FOR_SETUP" if status == "POTENTIAL_ENTRY" else status
    proposed = decision.proposed_score if decision else None
    gap = None if proposed is None or score is None else round(proposed - score, 2)
    risks = list(decision.risks) if decision else []
    if gap is not None and abs(gap) > cfg["score_discrepancy_threshold"]:
        risks.append(f"LLM score {proposed} differs from policy score {score} by {gap}: review")
    if policy["unavailable"]:
        risks.append("unavailable inputs: " + ", ".join(policy["unavailable"]))
    from app.scoring import validators  # local import avoids any import-order surprises
    risks += [f"validation: {flag}" for flag in validators.run_validations(results)]
    retrieved = [s["retrieved_at"] for r in results.values() for s in r.sources if s.get("retrieved_at") and s.get("status") == "ok"]
    summary = (decision.reasoning if decision else "") or "; ".join(f"{k}: {r.reasoning[:80]}" for k, r in results.items() if r.status == "ok")
    return StockResult(
        run_id=run_id, symbol=symbol, company_name=meta.get("company_name", ""), sub_sector=meta.get("sub_sector", ""), analysis_timestamp=as_of,
        scores={k: (results[k].score if usable(results.get(k)) else None) for k in AGENT_NAMES},
        agent_status={k: (results[k].status if k in results else "missing") for k in AGENT_NAMES},
        final_score=score, llm_proposed_score=proposed, score_discrepancy=gap,
        direction=_DIRECTION[policy["consensus_status"]], consensus_status=policy["consensus_status"], data_status=policy["data_status"],
        data_completeness=policy["data_completeness"], confidence=(decision.confidence if decision else 0.0),
        trade_status=status, status_adjustments=notes, reasoning_summary=summary[:600], risk_flags=risks,
        next_catalyst=decision.next_catalyst if decision else None,
        source_freshness=("oldest source retrieved " + min(retrieved)) if retrieved else "no sources retrieved",
        valid=valid, error=error, agent_results=results, decision=decision, weights_version=cfg["version"],
    )


def rank(stocks: list[StockResult]) -> list[StockResult]:
    """Valid results ranked by final_score (desc). Invalid ones are listed after, with no rank (never treated as 0)."""
    valid = sorted((s for s in stocks if s.valid), key=lambda s: (-(s.final_score or 0), s.symbol))
    invalid = sorted((s for s in stocks if not s.valid), key=lambda s: s.symbol)
    return valid + invalid
