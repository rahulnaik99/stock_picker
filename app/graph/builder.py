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
from typing import Any, Callable, Awaitable

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

# Required agents: if both attempts fail, the stock is skipped (saves running the remaining
# agents whose output would be discarded by the INSUFFICIENT_DATA gate anyway).
_REQUIRED_AGENTS = frozenset({"fundamental", "industry", "indicator"})


async def _run_with_retry(
    factory: Callable[[], Awaitable[Any]],
    agent_name: str,
    symbol: str,
    retry_delay: float = 2.0,
) -> Any:
    """Run `factory()` once; on exception wait and try once more.
    If the second attempt also fails:
    - required agents (fundamental/industry/indicator) → re-raise so the whole stock is skipped.
    - optional agents → return an error AgentResult so scoring can continue with partial data."""
    from app.schemas import AgentResult  # lazy to avoid circular at module level
    try:
        return await factory()
    except Exception as first_exc:  # noqa: BLE001
        log.warning("%s %s failed (attempt 1): %s — retrying in %.0fs",
                    agent_name, symbol, type(first_exc).__name__, retry_delay)
        await asyncio.sleep(retry_delay)
        try:
            return await factory()
        except Exception as second_exc:  # noqa: BLE001
            msg = (f"{agent_name} failed on both attempts — "
                   f"attempt1: {type(first_exc).__name__}: {first_exc} | "
                   f"attempt2: {type(second_exc).__name__}: {second_exc}")
            log.error("%s %s — %s", agent_name, symbol, msg[:300])
            if agent_name in _REQUIRED_AGENTS:
                raise RuntimeError(msg) from second_exc  # → stock skipped in runner.py
            return AgentResult(agent=agent_name, symbol=symbol, status="error", reasoning=msg[:300])


class _StockGraph:
    """Plain-asyncio stand-in for the old compiled LangGraph. Exposes `.ainvoke(state)`."""

    def __init__(self, client: ToolClient, cfg: dict[str, Any], ledger: EventLedger):
        self.client = client
        self.cfg = cfg
        self.ledger = ledger
        self.tech_weights = {k: cfg["weights"][k] for k in (
            "indicator", "trader", "rs_agent", "quant_risk",
            "promoter_intel", "catalyst_quality", "investment_trend", "option_chain")}

    # ── branch 1: fundamental (retry-once on failure) ─────────────────────
    async def _fundamental(self, state: dict[str, Any]) -> dict[str, AgentResult]:
        with log_context(agent="fundamental"):
            progress.agent_start("fundamental")
            sym = state["symbol"]
            r = await _run_with_retry(
                lambda: fundamental.run(sym, state["meta"], self.client.for_agent(fundamental.TOOLS), state["as_of"], self.ledger),
                "fundamental", sym)
            progress.agent_finish("fundamental", status="done" if r.status == "ok" else r.status,
                                  score=r.score, error=None if r.status == "ok" else r.reasoning[:200])
        return {"fundamental": r}

    # ── branch 2: industry (retry-once on failure) ─────────────────────────
    async def _industry(self, state: dict[str, Any]) -> dict[str, AgentResult]:
        with log_context(agent="industry"):
            progress.agent_start("industry")
            sym = state["symbol"]
            r = await _run_with_retry(
                lambda: industry.run(sym, state["meta"], self.client.for_agent(industry.TOOLS), state["as_of"]),
                "industry", sym)
            progress.agent_finish("industry", status="done" if r.status == "ok" else r.status,
                                  score=r.score, error=None if r.status == "ok" else r.reasoning[:200])
        return {"industry": r}

    # ── branch 3: technical (8 specialists; each retried once internally) ──
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
