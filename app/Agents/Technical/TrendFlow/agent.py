"""Investment Trend Agent: institutional/market participation. Market-wide and stock-specific evidence are kept apart."""
from __future__ import annotations

from typing import Any

from app.Agents.base import analyze, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.schemas import AgentResult
from app.scoring import baselines

NAME = "investment_trend"
TOOLS = ["nse_fii_dii", "nse_deals", "nse_shareholding"]
MARKET_WIDE_ONLY_CAP = 3.0  # aggregate FII/DII data cannot justify a strong stock-level score


async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    results = {
        "nse_fii_dii": await tools.call("nse_fii_dii", as_of=as_of),
        "nse_deals": await tools.call("nse_deals", symbol=symbol, days=30, as_of=as_of),
        "nse_shareholding": await tools.call("nse_shareholding", symbol=symbol),
    }
    market_ok = results["nse_fii_dii"]["status"] == "ok"
    deals = results["nse_deals"]
    stock_ok = (deals["status"] == "ok" and bool(deals["data"])) or results["nse_shareholding"]["status"] == "ok"
    # Evidence level is decided by code, never by the LLM.
    level = "stock_specific" if stock_ok else "market_wide_only" if market_ok else "none"
    if level == "none":
        # A successful but empty deals feed is real information (no block activity), still not a directional score.
        return unavailable_result(NAME, symbol, results, "No FII/DII, deal or shareholding evidence could be retrieved.")
    evidence = {k: v for k, v in results.items()}
    evidence["evidence_level"] = level
    sh = results["nse_shareholding"]["data"] if results["nse_shareholding"]["status"] == "ok" else None
    base_score, _parts = baselines.trend_baseline(results["nse_fii_dii"], sh, level)
    res = await analyze(agent=NAME, llm_key="trend_flow", symbol=symbol, system_prompt=load_prompt(__file__), evidence=evidence,
                        tool_results=results, context={**meta, "as_of": as_of}, baseline=base_score)
    if res.status == "ok":
        res.extra["evidence_level"] = level
        if level == "market_wide_only" and res.score is not None and abs(res.score) > MARKET_WIDE_ONLY_CAP:
            res.note = f"score capped from {res.score} to +/-{MARKET_WIDE_ONLY_CAP}: only market-wide flow data available"
            res.score = MARKET_WIDE_ONLY_CAP if res.score > 0 else -MARKET_WIDE_ONLY_CAP
        if level == "market_wide_only":
            res.missing_data.append("stock-specific flow evidence (deals / shareholding)")
    return res
