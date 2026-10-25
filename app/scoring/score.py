"""Deterministic scoring policy. Pure functions, no I/O, fully unit-tested.

The LLM never owns the official number: final_score is recomputed here from the specialist scores
with the configured weights. Missing data is never silently turned into zero.
"""
from __future__ import annotations

from typing import Any

from app.service.config import load_weights

AGENT_KEYS = ["fundamental", "industry", "indicators", "investment_trend", "option_chain"]


def usable_scores(results: dict[str, dict]) -> dict[str, float]:
    out = {}
    for k in AGENT_KEYS:
        r = results.get(k)
        if r and r.get("status") in ("ok", "partial") and r.get("score") is not None and -10 <= r["score"] <= 10:
            out[k] = float(r["score"])
    return out


def weighted_score(scores: dict[str, float], weights: dict[str, float]) -> tuple[float | None, float]:
    """-> (final_score, completeness). Weights of missing agents are dropped and the rest renormalised;
    completeness (= share of total weight present) is reported so partial results stay visible."""
    wsum = sum(weights[k] for k in scores)
    if wsum <= 0:
        return None, 0.0
    return round(sum(weights[k] * v for k, v in scores.items()) / wsum, 3), round(wsum / sum(weights.values()), 3)


def consensus(scores: dict[str, float], required: list[str]) -> str:
    """Strict unanimity gate over REQUIRED agents only."""
    if any(k not in scores for k in required):
        return "INSUFFICIENT_DATA"
    vals = [scores[k] for k in required]
    if all(v > 0 for v in vals):
        return "BULLISH_CONSENSUS"
    if all(v < 0 for v in vals):
        return "BEARISH_CONSENSUS"
    if any(v > 0 for v in vals) and any(v < 0 for v in vals):
        return "MIXED"
    return "NEUTRAL_OR_MIXED"


def finalize(stock: Any, results: dict[str, dict], verdict: Any | None, *, run_id: str, as_of: str,
             cfg: dict | None = None) -> dict:
    cfg = cfg or load_weights()
    scores = usable_scores(results)
    final, completeness = weighted_score(scores, cfg["weights"])
    cons = consensus(scores, cfg["required"])
    valid = cons != "INSUFFICIENT_DATA"
    flags: list[str] = []
    for k in AGENT_KEYS:
        r = results.get(k) or {}
        if k not in scores:
            flags.append(f"{k}: {r.get('status', 'missing')}" + (f" ({'; '.join(r['missing'])[:120]})" if r.get("missing") else ""))
        else:
            flags += [f"{k}: {f}" for f in (r.get("risk_flags") or [])[:3]]

    llm_score = verdict.final_score if verdict else None
    discrepancy = round(llm_score - final, 2) if (llm_score is not None and final is not None) else None
    if discrepancy is not None and abs(discrepancy) > cfg["discrepancy_threshold"]:
        flags.append(f"SCORE_DISCREPANCY: LLM {llm_score} vs calculated {final} (diff {discrepancy})")

    overext = bool((results.get("indicators") or {}).get("extra", {}).get("overextended") or (verdict and verdict.overextended))

    # direction: unanimity decides when it exists; otherwise the Decision Maker's judgement, sanity-checked.
    if not valid:
        direction = "INSUFFICIENT_DATA"
    elif cons == "BULLISH_CONSENSUS":
        direction = "BULLISH"
    elif cons == "BEARISH_CONSENSUS":
        direction = "BEARISH"
    else:
        direction = verdict.direction if verdict and verdict.direction in ("BULLISH", "BEARISH", "MIXED") else "MIXED"

    # trade status: the LLM proposes, hard rules can only make it MORE conservative.
    proposed = verdict.trade_status if verdict else "INSUFFICIENT_DATA"
    status, reason = proposed, None
    if not valid:
        status, reason = "INSUFFICIENT_DATA", "required agent scores missing"
    elif cons == "BEARISH_CONSENSUS" and proposed != "AVOID":
        status, reason = "AVOID", "bearish consensus"
    elif proposed == "POTENTIAL_ENTRY":
        if cons != "BULLISH_CONSENSUS":
            status, reason = "WAIT_FOR_SETUP", "no bullish consensus"
        elif final is None or final < cfg["entry_min_score"]:
            status, reason = "WAIT_FOR_SETUP", f"score below entry minimum {cfg['entry_min_score']}"
        elif completeness < cfg["min_completeness"]:
            status, reason = "WAIT_FOR_SETUP", f"data completeness {completeness} below {cfg['min_completeness']}"
        elif overext:
            status, reason = "WAIT_FOR_SETUP", "setup overextended"
    if verdict is None and valid:
        status, reason = "WAIT_FOR_SETUP", "decision maker unavailable"

    conf_vals = [results[k]["confidence"] for k in scores if results[k].get("confidence") is not None]
    confidence = round(min(((sum(conf_vals) / len(conf_vals)) if conf_vals else 0.0) * completeness,
                           verdict.confidence if verdict else 1.0), 3) if valid else 0.0

    freshness = "; ".join(f"{k}={results[k]['data_as_of'][:16]}" for k in AGENT_KEYS
                          if results.get(k) and results[k].get("data_as_of")) or "n/a"
    return {
        "rank": None, "symbol": stock.symbol, "company_name": stock.company_name, "sector_group": stock.sector_group,
        "analysis_timestamp": as_of,
        **{f"{k}_score": scores.get(k) for k in AGENT_KEYS},
        "final_score": final if valid else None, "direction": direction, "consensus_status": cons,
        "confidence": confidence, "trade_status": status,
        "reasoning_summary": (verdict.reasoning_summary if verdict else "No decision-maker output."),
        "risk_flags": " | ".join(dict.fromkeys(flags + (verdict.risk_flags if verdict else []))),
        "next_catalyst": (verdict.next_catalyst if verdict else None) or "",
        "data_completeness": completeness, "source_freshness": freshness, "run_id": run_id,
        # audit extras
        "valid": valid, "llm_final_score": llm_score, "score_discrepancy": discrepancy,
        "status_proposed": proposed, "status_override_reason": reason if status != proposed else None,
        "overextended": overext, "invalidation": (verdict.invalidation if verdict else None) or "",
    }


def rank(finals: list[dict]) -> list[dict]:
    """Rank valid results by final_score (desc). Invalid ones keep rank None and are listed after."""
    valid = sorted((f for f in finals if f["valid"] and f["final_score"] is not None),
                   key=lambda f: (-f["final_score"], f["symbol"]))
    for i, f in enumerate(valid, 1):
        f["rank"] = i
    invalid = sorted((f for f in finals if f not in valid), key=lambda f: f["symbol"])
    for f in invalid:
        f["rank"] = None
    return valid + invalid
