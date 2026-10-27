"""Quant Risk Agent: beta, drawdown profile, VaR, volatility — position sizing context."""
from __future__ import annotations
from typing import Any
from app.Agents.base import analyze, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.schemas import AgentResult
from app.scoring import baselines

NAME = "quant_risk"
TOOLS = ["get_quant_risk"]

async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    risk = await tools.call("get_quant_risk", symbol=symbol, as_of=as_of)
    results = {"get_quant_risk": risk}
    if risk["status"] != "ok":
        return unavailable_result(NAME, symbol, results, risk.get("message") or "risk data unavailable")
    data = risk["data"]
    base, parts = baselines.quant_risk_baseline(data)
    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__),
                        evidence={"risk": data}, tool_results=results,
                        context={**meta, "as_of": as_of}, baseline=base)
    if res.status == "ok":
        res.extra.update({"beta": data.get("beta_vs_nifty"), "risk_level": data.get("risk_level"),
                          "max_dd_3m": (data.get("max_drawdown_pct") or {}).get("3m"),
                          "position_size_factor": data.get("position_size_factor"),
                          "baseline_parts": parts})
    return res
