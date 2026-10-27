"""Promoter Intelligence Agent: pledge risk, stake trend, insider signals — India-specific."""
from __future__ import annotations
from typing import Any
from app.Agents.base import analyze, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.schemas import AgentResult
from app.scoring import baselines

NAME = "promoter_intel"
TOOLS = ["nse_shareholding", "nse_promoter_pledge", "nse_company_events"]

async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    import asyncio
    sh_task  = tools.call("nse_shareholding", symbol=symbol)
    pl_task  = tools.call("nse_promoter_pledge", symbol=symbol)
    ev_task  = tools.call("nse_company_events", symbol=symbol, days=120, as_of=as_of)
    sh_data, pl_data, ev_data = await asyncio.gather(sh_task, pl_task, ev_task)
    results = {"nse_shareholding": sh_data, "nse_promoter_pledge": pl_data, "nse_company_events": ev_data}
    if sh_data["status"] != "ok" and pl_data["status"] != "ok":
        return unavailable_result(NAME, symbol, results, "No shareholding or pledging data available")
    sh_rows = sh_data.get("data") if sh_data["status"] == "ok" else None
    pl_rows = pl_data.get("data") if pl_data["status"] == "ok" else None
    # Extract insider events (management changes, buybacks, fundraise)
    insider_events = [{"type": e.get("event_type"), "date": e.get("event_date"),
                       "summary": (e.get("event_summary") or "")[:80]}
                      for e in (ev_data.get("data") or [])
                      if e.get("event_type") in ("management_change", "buyback", "fundraise", "merger_acquisition")][:5]
    base, parts = baselines.promoter_baseline(pl_rows, sh_rows)
    evidence = {"shareholding": sh_rows, "pledge": pl_rows, "insider_events": insider_events}
    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__),
                        evidence=evidence, tool_results=results,
                        context={**meta, "as_of": as_of}, baseline=base)
    if res.status == "ok":
        res.extra.update({
            "pledge_risk": (pl_rows or {}).get("risk") if isinstance(pl_rows, dict) else None,
            "pledge_trend": (pl_rows or {}).get("trend") if isinstance(pl_rows, dict) else None,
            "latest_pledge_pct": (pl_rows or {}).get("latest_pledge_pct") if isinstance(pl_rows, dict) else None,
            "baseline_parts": parts,
        })
        if pl_data["status"] != "ok":
            res.missing_data.append("promoter pledging data (NSE endpoint unavailable)")
    return res
