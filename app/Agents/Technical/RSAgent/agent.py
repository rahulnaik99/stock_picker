"""Relative Strength Agent: is this stock leading or lagging its sector and the market?"""
from __future__ import annotations
from typing import Any
from app.Agents.base import analyze, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.schemas import AgentResult
from app.scoring import baselines

NAME = "rs_agent"
TOOLS = ["get_relative_strength"]

async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    rs = await tools.call("get_relative_strength", symbol=symbol,
                          sub_sector=meta.get("sub_sector"), as_of=as_of)
    results = {"get_relative_strength": rs}
    if rs["status"] != "ok":
        return unavailable_result(NAME, symbol, results, rs.get("message") or "RS data unavailable")
    data = rs["data"]
    base, parts = baselines.rs_baseline(data)
    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__),
                        evidence={"rs": data}, tool_results=results,
                        context={**meta, "as_of": as_of}, baseline=base)
    if res.status == "ok":
        res.extra.update({"signal": data.get("signal"), "rs_trend": data.get("rs_trend"),
                          "rs_vs_sector_13w": (data.get("rs_vs_sector_pct") or {}).get("13w"),
                          "baseline_parts": parts})
    return res
