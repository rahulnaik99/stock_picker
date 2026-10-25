"""LangGraph for ONE stock.

START -> fundamental ─┐
START -> industry ────┼─> decision -> finalize -> END
START -> technical ───┘   (technical = indicator + investment_trend + option_chain, run concurrently)
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.Agents.DecisionMaker import agent as decision_maker
from app.Agents.Fundamental import agent as fundamental
from app.Agents.Industry import agent as industry
from app.Agents.Technical import agent as technical
from app.ledger.events import EventLedger
from app.mcp_server.client import ToolClient
from app.schemas import AgentResult, StockResult
from app.scoring import policy
from app.service.logger import get_logger, log_context

log = get_logger(__name__)


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class StockState(TypedDict, total=False):
    run_id: str
    symbol: str
    meta: dict[str, Any]
    as_of: str
    results: Annotated[dict[str, AgentResult], _merge]
    decision: Any
    final: StockResult


def build_graph(client: ToolClient, cfg: dict[str, Any], ledger: EventLedger | None = None):
    ledger = ledger or EventLedger()
    tech_weights = {k: cfg["weights"][k] for k in ("indicator", "investment_trend", "option_chain")}

    async def fundamental_node(state: StockState):
        with log_context(agent="fundamental"):
            try:
                r = await fundamental.run(state["symbol"], state["meta"], client.for_agent(fundamental.TOOLS), state["as_of"], ledger)
            except Exception as exc:  # noqa: BLE001
                log.exception("fundamental failed")
                r = AgentResult(agent="fundamental", symbol=state["symbol"], status="error", reasoning=f"{type(exc).__name__}: {exc}")
        return {"results": {"fundamental": r}}

    async def industry_node(state: StockState):
        with log_context(agent="industry"):
            try:
                r = await industry.run(state["symbol"], state["meta"], client.for_agent(industry.TOOLS), state["as_of"])
            except Exception as exc:  # noqa: BLE001
                log.exception("industry failed")
                r = AgentResult(agent="industry", symbol=state["symbol"], status="error", reasoning=f"{type(exc).__name__}: {exc}")
        return {"results": {"industry": r}}

    async def technical_node(state: StockState):
        res = await technical.run(state["symbol"], state["meta"], client, state["as_of"], tech_weights)
        log.info("technical consolidated score = %s", technical.consolidated_technical_score(res, tech_weights))
        return {"results": res}

    async def decision_node(state: StockState):
        with log_context(agent="decision_maker"):
            pol = policy.compute_policy(state["results"], cfg)
            log.info("policy: score=%s consensus=%s data=%s completeness=%s", pol["policy_score"], pol["consensus_status"], pol["data_status"], pol["data_completeness"])
            if pol["consensus_status"] == "INSUFFICIENT_DATA":
                return {"decision": None}  # nothing to reconcile; the data gate decides
            decision, provider, model = await decision_maker.run(state["symbol"], state["meta"], state["results"], pol, state["as_of"])
            return {"decision": decision}

    async def finalize_node(state: StockState):
        res = policy.finalize(run_id=state["run_id"], symbol=state["symbol"], meta=state["meta"], as_of=state["as_of"],
                              results=state["results"], decision=state.get("decision"), cfg=cfg)
        log.info("FINAL %s score=%s direction=%s status=%s", state["symbol"], res.final_score, res.direction, res.trade_status)
        return {"final": res}

    g = StateGraph(StockState)
    g.add_node("fundamental", fundamental_node)
    g.add_node("industry", industry_node)
    g.add_node("technical", technical_node)
    g.add_node("decision", decision_node)
    g.add_node("finalize", finalize_node)
    for n in ("fundamental", "industry", "technical"):
        g.add_edge(START, n)
    g.add_edge(["fundamental", "industry", "technical"], "decision")
    g.add_edge("decision", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
