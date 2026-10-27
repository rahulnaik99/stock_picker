"""Option Chain Agent: OI / PCR positioning for F&O stocks. Unavailable (not neutral) when there is no chain."""
from __future__ import annotations

from typing import Any

from app.Agents.base import analyze, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.schemas import AgentResult
from app.scoring import baselines

NAME = "option_chain"
TOOLS = ["get_option_chain"]


async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    chain = await tools.call("get_option_chain", symbol=symbol, strikecount=10, as_of=as_of)
    results = {"get_option_chain": chain}
    if chain["status"] != "ok":
        return unavailable_result(NAME, symbol, results, chain.get("message") or "no option chain")
    base_score, baseline_parts = baselines.option_baseline(chain["data"])
    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__), evidence={"option_chain": chain["data"]},
                        tool_results=results, context={**meta, "as_of": as_of}, baseline=base_score)
    if res.status == "ok":
        windows = (chain["data"] or {}).get("windows", {})
        if not windows or all(v == "insufficient_history" for v in windows.values()):
            res.missing_data.append("weekly/monthly OI change (needs saved daily snapshots; builds up over repeated runs)")
        res.extra["baseline_parts"] = baseline_parts
    return res
