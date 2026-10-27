"""Technical Analyst = coordinator. Runs its three specialists concurrently and consolidates a technical score.

Specialist results stay separate (the final weighted score uses indicator / investment_trend / option_chain individually).
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.Agents.Technical.Indicator import agent as indicator
from app.Agents.Technical.OptionChain import agent as option_chain
from app.Agents.Technical.Trader import agent as trader
from app.Agents.Technical.TrendFlow import agent as trend_flow
from app.Agents.Technical.RSAgent import agent as rs_agent
from app.Agents.Technical.QuantRisk import agent as quant_risk
from app.Agents.Technical.PromoterIntel import agent as promoter_intel
from app.Agents.Technical.CatalystQuality import agent as catalyst_quality
from app.mcp_server.client import ToolClient
from app.schemas import AgentResult
from app.service import progress
from app.service.logger import log_context

SPECIALISTS = {
    "indicator":        indicator,
    "trader":           trader,
    "rs_agent":         rs_agent,
    "quant_risk":       quant_risk,
    "promoter_intel":   promoter_intel,
    "catalyst_quality": catalyst_quality,
    "investment_trend": trend_flow,
    "option_chain":     option_chain,
}


async def run(symbol: str, meta: dict[str, Any], client: ToolClient, as_of: str | None, technical_weights: dict[str, float]) -> dict[str, AgentResult]:
    async def one(key: str, mod) -> AgentResult:
        with log_context(agent=key):
            progress.agent_start(key)
            try:
                r = await mod.run(symbol, meta, client.for_agent(mod.TOOLS), as_of)
            except Exception as exc:  # noqa: BLE001 - one specialist must not sink the stock
                r = AgentResult(agent=key, symbol=symbol, status="error", reasoning=f"{type(exc).__name__}: {exc}")
            progress.agent_finish(key, status="done" if r.status == "ok" else r.status, score=r.score,
                                  error=None if r.status == "ok" else r.reasoning[:200])
            return r

    results = dict(zip(SPECIALISTS, await asyncio.gather(*(one(k, m) for k, m in SPECIALISTS.items()))))
    return results


def consolidated_technical_score(results: dict[str, AgentResult], weights: dict[str, float]) -> float | None:
    """Weighted mean of the available technical specialists (None when none is usable)."""
    pairs = [(weights[k], r.score) for k, r in results.items() if k in weights and r.status == "ok" and r.score is not None]
    total = sum(w for w, _ in pairs)
    return round(sum(w * s for w, s in pairs) / total, 2) if total else None
