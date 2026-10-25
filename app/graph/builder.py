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
from app.scoring import validators
from app.service import progress
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
            progress.agent_start("fundamental")
            try:
                r = await fundamental.run(state["symbol"], state["meta"], client.for_agent(fundamental.TOOLS), state["as_of"], ledger)
            except Exception as exc:  # noqa: BLE001
                log.exception("fundamental failed")
                r = AgentResult(agent="fundamental", symbol=state["symbol"], status="error", reasoning=f"{type(exc).__name__}: {exc}")
            progress.agent_finish("fundamental", status="done" if r.status == "ok" else r.status, score=r.score,
                                  error=None if r.status == "ok" else r.reasoning[:200])
        return {"results": {"fundamental": r}}

    async def industry_node(state: StockState):
        with log_context(agent="industry"):
            progress.agent_start("industry")
            try:
                r = await industry.run(state["symbol"], state["meta"], client.for_agent(industry.TOOLS), state["as_of"])
            except Exception as exc:  # noqa: BLE001
                log.exception("industry failed")
                r = AgentResult(agent="industry", symbol=state["symbol"], status="error", reasoning=f"{type(exc).__name__}: {exc}")
            progress.agent_finish("industry", status="done" if r.status == "ok" else r.status, score=r.score,
                                  error=None if r.status == "ok" else r.reasoning[:200])
        return {"results": {"industry": r}}

    async def technical_node(state: StockState):
        res = await technical.run(state["symbol"], state["meta"], client, state["as_of"], tech_weights)
        log.info("technical consolidated score = %s", technical.consolidated_technical_score(res, tech_weights))
        return {"results": res}

    async def decision_node(state: StockState):
        with log_context(agent="decision_maker"):
            pol = policy.compute_policy(state["results"], cfg)
            pol["validation_flags"] = validators.run_validations(state["results"])
            log.info("policy: score=%s consensus=%s data=%s completeness=%s flags=%d", pol["policy_score"], pol["consensus_status"],
                     pol["data_status"], pol["data_completeness"], len(pol["validation_flags"]))
            if pol["consensus_status"] == "INSUFFICIENT_DATA":
                progress.agent_finish("decision_maker", status="skipped", error="insufficient data: nothing to reconcile")
                return {"decision": None}  # nothing to reconcile; the data gate decides
            progress.agent_start("decision_maker")
            decision, provider, model = await decision_maker.run(state["symbol"], state["meta"], state["results"], pol, state["as_of"])
            progress.agent_finish("decision_maker", status="done" if decision is not None else "error",
                                  score=decision.proposed_score if decision else None,
                                  error=None if decision is not None else "decision maker returned no usable output")
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
