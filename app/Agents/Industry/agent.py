"""Industry Analyst: ONE sector-direction score for all Indian financial stocks (banks, NBFCs, insurers, AMCs, exchanges).

The score is computed once per day (first stock that needs it), saved in SQLite, and reused by every stock in the run and by later
runs on the same day. It describes the sector backdrop (RBI and foreign rates, liquidity, credit, regulation, flows), not a company.
"""
from __future__ import annotations

import asyncio
import json
import weakref
from typing import Any

from app.Agents.base import analyze, compact, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.mcp_server.tools.common import parse_as_of
from app.schemas import AgentResult
from app.scoring import baselines
from app.service.logger import get_logger
from app.service.settings import ROOT, get_settings
from app.store.db import kv_get, kv_set

log = get_logger(__name__)
NAME = "industry"
SCOPE = "ALL_FINANCIALS"
# Bump when the industry prompt or baseline changes so a stale shared score isn't reused the same day.
SCORE_CACHE_VERSION = "v2"
TOOLS = ["rbi_policy", "foreign_rates", "macro_snapshot", "policy_calendar", "news_search"]
CFG = json.loads((ROOT / "app" / "config" / "sectors.json").read_text())["_financials_shared"]
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


async def _compute(tools: AgentTools, as_of: str | None) -> AgentResult:
    results: dict[str, dict[str, Any]] = {
        "rbi_policy": await tools.call("rbi_policy", days=45, as_of=as_of),
        "foreign_rates": await tools.call("foreign_rates", as_of=as_of),
        "policy_calendar": await tools.call("policy_calendar", days_ahead=45, as_of=as_of),
        "macro_snapshot": await tools.call("macro_snapshot", as_of=as_of),
    }
    news = [await tools.call("news_search", query=q, days=14, as_of=as_of) for q in CFG["news_queries"]]
    results["sector_news"] = {"status": "ok" if any(n["status"] == "ok" for n in news) else news[0]["status"],
                              "source": "google_news_rss", "retrieved_at": news[0].get("retrieved_at"),
                              "data": [i for n in news if n["status"] == "ok" for i in n["data"]][:24],
                              "message": "; ".join(sorted({n.get("message") or "" for n in news if n["status"] != "ok"}))[:200]}
    if all(r["status"] != "ok" for r in results.values()):
        return unavailable_result(NAME, SCOPE, results, "No sector, rates, macro or news data could be retrieved.")
    evidence = {k: compact(v, 12) for k, v in results.items()}
    evidence["tool_status"] = {k: v["status"] for k, v in results.items()}
    macro = results["macro_snapshot"]["data"] if results["macro_snapshot"]["status"] == "ok" else []
    base_score, _parts = baselines.industry_baseline(macro)
    return await analyze(agent=NAME, llm_key=NAME, symbol=SCOPE, system_prompt=load_prompt(__file__, sector_focus=CFG["focus"]),
                         evidence=evidence, tool_results=results, context={"scope": SCOPE, "as_of": as_of}, baseline=base_score)


async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    day = f"{parse_as_of(as_of).date().isoformat()}:{SCORE_CACHE_VERSION}"
    async with _lock():                       # parallel stocks wait here; the first one computes, the rest reuse
        cached = None if get_settings().force_refresh else kv_get("industry_score", day)
        if cached:
            log.info("industry: reusing shared score for %s (%s)", day, cached.get("score"))
            res = AgentResult(**cached)
            note = "shared sector score (cached for this date)"
        else:
            res = await _compute(tools, as_of)
            note = "shared sector score (computed now)"
            if res.status == "ok":
                kv_set("industry_score", day, res.model_dump())
    return res.model_copy(update={"symbol": symbol, "note": (res.note + " " + note).strip()})
