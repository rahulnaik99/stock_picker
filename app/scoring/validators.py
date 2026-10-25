"""Deterministic post-analysis validation layer. No LLM calls.

Turns silent data problems into explicit, plain-English flags that are (a) shown to the Decision Maker so it
cannot quietly let stale/missing data raise a verdict, and (b) recorded on the final result's risk_flags.

Everything here is pure Python and unit-tested.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.schemas import AgentResult

# A 'results' filing this many days newer than the latest quarter in the stored series implies a reported
# quarter is missing from the numbers (results are normally public within ~45 days of quarter end).
RESULT_LAG_DAYS = 55


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def results_freshness(events: list[dict[str, Any]], latest_period: str | None, lag_days: int = RESULT_LAG_DAYS) -> str | None:
    """Flag when a 'results' event is materially newer than the latest quarter present in the numeric series.
    This is the guard against narrating the latest *announced* quarter using older series figures."""
    lp = _as_date(latest_period)
    if lp is None:
        return None
    dates = [d for e in (events or []) if e.get("event_type") == "results" for d in (_as_date(e.get("event_date")),) if d]
    if not dates:
        return None
    newest = max(dates)
    if (newest - lp).days > lag_days:
        return (f"STALE FUNDAMENTALS: a results filing dated {newest.isoformat()} is newer than the latest quarter in the "
                f"numeric series ({lp.isoformat()}). The newer quarter's actuals are NOT in these numbers — treat that "
                f"quarter as not-yet-filed and never attach the series figures to it.")
    return None


def agent_flags(r: AgentResult | None) -> list[str]:
    """Self-consistency checks for one specialist result."""
    out: list[str] = []
    if r is None:
        return out
    if r.status == "ok":
        if r.score is None:
            out.append(f"{r.agent}: status 'ok' but score is null")
        elif not -10 <= r.score <= 10:
            out.append(f"{r.agent}: score {r.score} outside [-10, 10]")
        if not 0 <= (r.confidence or 0) <= 1:
            out.append(f"{r.agent}: confidence {r.confidence} outside [0, 1]")
        if r.score is not None and abs(r.score) >= 4 and not r.evidence:
            out.append(f"{r.agent}: strong score {r.score} with no evidence cited")
    elif r.score is not None:
        out.append(f"{r.agent}: status '{r.status}' (no data) but score is not null ({r.score})")
    return out


def run_validations(results: dict[str, AgentResult]) -> list[str]:
    """Aggregate all deterministic flags for a stock (per-agent consistency + the fundamental freshness flag)."""
    flags: list[str] = []
    for r in results.values():
        flags += agent_flags(r)
    fund = results.get("fundamental")
    if fund is not None:
        fresh = (fund.extra or {}).get("data_freshness_flag")
        if fresh:
            flags.append(fresh)
    return flags
