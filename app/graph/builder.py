"""Single-stock scoring orchestration.

START ─┬─ fundamental ─┐
       ├─ industry ────┼─> decision -> finalize -> END
       └─ technical ───┘   (technical = indicator + trader + rs_agent + quant_risk +
                            promoter_intel + catalyst_quality + investment_trend + option_chain)

The three top branches run concurrently; decision waits for all of them, then finalize.
This is plain asyncio — NO langgraph. langgraph 1.x shipped an internal import break
(`langgraph.checkpoint.serde.jsonplus` importing `Item` from `langgraph.store.base`),
and this graph needs none of langgraph's features (no persistence, no checkpointing,
no streaming), so we orchestrate it directly. The returned object keeps the historical
`.ainvoke(state)` interface so runner.py is unchanged.
"""
from __future__ import annotations

import asyncio
from typing import Any

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


class _StockGraph:
    """Plain-asyncio stand-in for the old compiled LangGraph. Exposes `.ainvoke(state)`."""

    def __init__(self, client: ToolClient, cfg: dict[str, Any], ledger: EventLedger):
        self.client = client
        self.cfg = cfg
        self.ledger = ledger
        self.tech_weights = {k: cfg["weights"][k] for k in (
            "indicator", "trader", "rs_agent", "quant_risk",
            "promoter_intel", "catalyst_quality", "investment_trend", "option_chain")}

    # ── branch 1: fundamental ──────────────────────────────────────────────
    async def _fundamental(self, state: dict[str, Any]) -> dict[str, AgentResult]:
        with log_context(agent="fundamental"):
            progress.agent_start("fundamental")
            try:
                r = await fundamental.run(state["symbol"], state["meta"],
                                          self.client.for_agent(fundamental.TOOLS), state["as_of"], self.ledger)
            except Exception as exc:  # noqa: BLE001
                log.exception("fundamental failed")
                r = AgentResult(agent="fundamental", symbol=state["symbol"], status="error",
                                reasoning=f"{type(exc).__name__}: {exc}")
            progress.agent_finish("fundamental", status="done" if r.status == "ok" else r.status, score=r.score,
                                  error=None if r.status == "ok" else r.reasoning[:200])
        return {"fundamental": r}

    # ── branch 2: industry ─────────────────────────────────────────────────
    async def _industry(self, state: dict[str, Any]) -> dict[str, AgentResult]:
        with log_context(agent="industry"):
            progress.agent_start("industry")
            try:
                r = await industry.run(state["symbol"], state["meta"],
                                       self.client.for_agent(industry.TOOLS), state["as_of"])
            except Exception as exc:  # noqa: BLE001
                log.exception("industry failed")
                r = AgentResult(agent="industry", symbol=state["symbol"], status="error",
                                reasoning=f"{type(exc).__name__}: {exc}")
            progress.agent_finish("industry", status="done" if r.status == "ok" else r.status, score=r.score,
                                  error=None if r.status == "ok" else r.reasoning[:200])
        return {"industry": r}

    # ── branch 3: technical (8 specialists run concurrently inside) ─────────
    async def _technical(self, state: dict[str, Any]) -> dict[str, AgentResult]:
        res = await technical.run(state["symbol"], state["meta"], self.client, state["as_of"], self.tech_weights)
        log.info("technical consolidated score = %s",
                 technical.consolidated_technical_score(res, self.tech_weights))
        return res

    async def _decision(self, state: dict[str, Any]) -> Any:
        with log_context(agent="decision_maker"):
            pol = policy.compute_policy(state["results"], self.cfg)
            pol["validation_flags"] = validators.run_validations(state["results"])
            log.info("policy: score=%s consensus=%s data=%s completeness=%s flags=%d",
                     pol["policy_score"], pol["consensus_status"], pol["data_status"],
                     pol["data_completeness"], len(pol["validation_flags"]))
            if pol["consensus_status"] == "INSUFFICIENT_DATA":
                progress.agent_finish("decision_maker", status="skipped",
                                      error="insufficient data: nothing to reconcile")
                return None
            progress.agent_start("decision_maker")
            decision, provider, model = await decision_maker.run(
                state["symbol"], state["meta"], state["results"], pol, state["as_of"])
            progress.agent_finish("decision_maker", status="done" if decision is not None else "error",
                                  score=decision.proposed_score if decision else None,
                                  error=None if decision is not None else "decision maker returned no usable output")
            return decision

    def _finalize(self, state: dict[str, Any]) -> StockResult:
        res = policy.finalize(run_id=state["run_id"], symbol=state["symbol"], meta=state["meta"],
                              as_of=state["as_of"], results=state["results"],
                              decision=state.get("decision"), cfg=self.cfg)
        log.info("FINAL %s score=%s direction=%s status=%s",
                 state["symbol"], res.final_score, res.direction, res.trade_status)
        return res

    async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
        # ① three branches concurrently (same as START -> {fundamental, industry, technical})
        fund, ind, tech = await asyncio.gather(
            self._fundamental(state), self._industry(state), self._technical(state))
        results: dict[str, AgentResult] = {**fund, **ind, **tech}
        state = {**state, "results": results}
        # ② decision waits for all branches
        state = {**state, "decision": await self._decision(state)}
        # ③ finalize
        state = {**state, "final": self._finalize(state)}
        return state


def build_graph(client: ToolClient, cfg: dict[str, Any], ledger: EventLedger | None = None):
    """Return an object with `.ainvoke(state)` — plain asyncio, no langgraph dependency."""
    return _StockGraph(client, cfg, ledger or EventLedger())
