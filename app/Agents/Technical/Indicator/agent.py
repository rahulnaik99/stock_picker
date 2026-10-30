"""Technical Indicator Agent. Numbers come from code (get_technical_indicators); the LLM interprets them."""
from __future__ import annotations

from typing import Any

from app.Agents.base import analyze, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.schemas import AgentResult

NAME = "indicator"
TOOLS = ["get_technical_indicators"]


async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    ind = await tools.call("get_technical_indicators", symbol=symbol, as_of=as_of)
    results = {"get_technical_indicators": ind}
    if ind["status"] != "ok":
        return unavailable_result(NAME, symbol, results, ind.get("message") or "indicators unavailable")
    data = ind["data"]
    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__), evidence={"indicators": data},
                        tool_results=results, context={**meta, "as_of": as_of}, baseline=data["rule_score"])
    if res.status == "ok":
        # Hard facts from code win over anything the LLM says.
        res.extra.update({"extended": data["extended"], "rule_score": data["rule_score"], "levels": {
            "support": data["support"], "resistance": data["resistance"], "close": data["close"], "atr14": data["atr14"]},
            "candle_provider": data.get("candle_provider")})
        if data.get("vwap_session") is None:
            res.missing_data.append("session VWAP (no intraday volume)")
    return res
