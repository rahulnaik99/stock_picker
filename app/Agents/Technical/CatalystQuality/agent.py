"""Catalyst Quality Agent: classify events and news quality beyond keyword counting.

Uses the configured LLM (optionally nimble-v3 for speed) with a tight classification prompt.
Produces a composite catalyst quality label + per-event scores that the Decision Maker can cite.
"""
from __future__ import annotations
from typing import Any
from app.Agents.base import analyze, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.schemas import AgentResult
from app.scoring import baselines

NAME = "catalyst_quality"
TOOLS = ["nse_company_events", "news_search"]

_POS = ("profit","growth","beat","upgrade","strong","record","surge","raise","dividend","buyback","expansion")
_NEG = ("loss","decline","fall","downgrade","weak","fraud","penalty","probe","miss","npa","default","resign")

def _quick_lean(text: str) -> str:
    t = text.lower()
    pos = sum(w in t for w in _POS)
    neg = sum(w in t for w in _NEG)
    return "positive" if pos > neg else "negative" if neg > pos else "neutral"


def _classify_events(events: list[dict], news: list[dict]) -> dict[str, Any]:
    """Fast deterministic pre-classification (used as baseline and as fallback)."""
    scored = []
    for e in (events or [])[:10]:
        s = (e.get("event_summary") or "")
        lean = _quick_lean(s)
        weight = {"results": 3, "dividend": 1, "fundraise": 1, "management_change": 2,
                  "buyback": 2, "credit_rating": 2, "merger_acquisition": 2}.get(e.get("event_type",""), 1)
        scored.append({"type": e.get("event_type"), "date": e.get("event_date"),
                       "lean": lean, "weight": weight, "text": s[:80]})
    news_scored = [{"lean": _quick_lean(n.get("title","")), "title": (n.get("title",""))[:80]}
                   for n in (news or [])[:8]]
    total_w = sum(s["weight"] for s in scored) or 1
    score = sum(s["weight"] * (1 if s["lean"]=="positive" else -1 if s["lean"]=="negative" else 0)
                for s in scored)
    news_score = sum(1 if n["lean"]=="positive" else -1 if n["lean"]=="negative" else 0
                     for n in news_scored)
    composite = score + news_score * 0.3
    if composite > 3:   label = "strong_positive"
    elif composite > 1: label = "positive"
    elif composite > 0: label = "mildly_positive"
    elif composite < -3: label = "strong_negative"
    elif composite < -1: label = "negative"
    elif composite < 0: label = "mildly_negative"
    else: label = "mixed"
    return {"composite_quality": label, "composite_score": round(composite, 2),
            "event_scores": scored, "news_scores": news_scored,
            "note": "pre-classified; LLM refines with context"}


async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    import asyncio
    company = meta.get("company_name") or symbol
    ev_task = tools.call("nse_company_events", symbol=symbol, days=120, as_of=as_of)
    ns_task = tools.call("news_search", query=f'"{company}" results OR dividend OR rating',
                         days=14, as_of=as_of)
    ev_data, ns_data = await asyncio.gather(ev_task, ns_task)
    results = {"nse_company_events": ev_data, "news_search": ns_data}
    if ev_data["status"] != "ok" and ns_data["status"] != "ok":
        return unavailable_result(NAME, symbol, results, "No event or news data for catalyst quality scoring")
    events = ev_data.get("data") or [] if ev_data["status"] == "ok" else []
    news   = ns_data.get("data") or [] if ns_data["status"] == "ok" else []
    catalyst = _classify_events(events, news)
    base, parts = baselines.catalyst_quality_baseline(catalyst)
    evidence = {"catalyst_classification": catalyst, "recent_events": events[:8], "news": news[:8]}
    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__),
                        evidence=evidence, tool_results=results,
                        context={**meta, "as_of": as_of}, baseline=base)
    if res.status == "ok":
        res.extra.update({"composite_quality": catalyst.get("composite_quality"),
                          "composite_score": catalyst.get("composite_score"),
                          "baseline_parts": parts})
    return res
