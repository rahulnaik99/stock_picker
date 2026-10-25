"""Trader Agent: a bull/bear price-action desk. Zones come from code (get_demand_supply_zones); the LLM argues
the long case and the short case around them and nets a bounded adjustment. Distinct from the Indicator agent
(EMA/RSI/MACD scoring) - this one reasons in demand/supply, EMA, VWAP and price action."""
from __future__ import annotations

from typing import Any

from app.Agents.base import analyze, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.schemas import AgentResult
from app.scoring import baselines

NAME = "trader"
TOOLS = ["get_demand_supply_zones"]


async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    z = await tools.call("get_demand_supply_zones", symbol=symbol, as_of=as_of)
    results = {"get_demand_supply_zones": z}
    if z["status"] != "ok":
        return unavailable_result(NAME, symbol, results, z.get("message") or "demand/supply zones unavailable")
    data = z["data"]
    base, parts = baselines.trader_baseline(data)
    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__),
                        evidence={"zones": data}, tool_results=results, context={**meta, "as_of": as_of}, baseline=base)
    if res.status == "ok":
        # Hard facts from code win over anything the LLM says.
        res.extra.update({
            "bull_score": parts.get("bull_score"), "bear_score": parts.get("bear_score"),
            "ema_stack": data.get("ema_stack"), "vwap_side": data.get("vwap_side"),
            "position": (data.get("price_action") or {}).get("position"),
            "nearest_demand": data.get("nearest_demand"), "nearest_supply": data.get("nearest_supply"),
            "mtf": data.get("mtf"), "trade_plan": data.get("trade_plan"),
            "candle_provider": data.get("candle_provider"),
        })
        if data.get("vwap_session") is None:
            res.missing_data.append("session VWAP (no intraday volume)")
    return res
