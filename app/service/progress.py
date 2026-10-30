"""In-memory, thread-safe live progress + token tracking for a scoring run.

The scoring graph runs many agents concurrently per stock. Historically the only record of
"which agent finished" was written to SQLite *after the whole run completed* (run_agent_io),
so a UI could not show live progress. This module fills that gap:

* `init_run`        - register a run with its symbols and the agents each stock will execute.
* `agent_start` / `agent_finish` - mark an agent pending -> running -> done/error/skipped.
* `record_tokens`   - accumulate provider (or estimated) token usage per agent.
* `snapshot`        - a JSON-serialisable view the API exposes for polling.

All mutating helpers default to reading the current run_id / symbol from the logging context
vars, so call sites inside graph nodes and agents do not need to thread identifiers through.

Only the most recent `MAX_RUNS` runs are kept in memory (bounded), and final token totals are
also persisted to the `kv` table so the API can still report them after a restart.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from app.mcp_server.tools.common import now_iso
from app.service.logger import run_id_var, symbol_var, agent_var

# Agents tracked for live display, in pipeline order. "decision_maker" runs only when the data gate passes.
AGENT_ORDER: list[str] = ["fundamental", "industry", "indicator", "trader",
                          "rs_agent", "quant_risk", "promoter_intel", "catalyst_quality",
                          "investment_trend", "option_chain", "decision_maker"]
MAX_RUNS = 50

_lock = threading.RLock()
_runs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _new_agent() -> dict[str, Any]:
    return {"status": "pending", "started_at": None, "finished_at": None, "duration_ms": None,
            "score": None, "provider": None, "model": None, "input_tokens": 0, "output_tokens": 0, "error": None}


def _run_id() -> str:
    return run_id_var.get()


def init_run(run_id: str, symbols: list[str], agents: list[str] | None = None) -> None:
    agents = agents or AGENT_ORDER
    with _lock:
        _runs[run_id] = {
            "run_id": run_id, "status": "running", "created_at": now_iso(), "finished_at": None,
            "agents": list(agents),
            "symbols": {s: {a: _new_agent() for a in agents} for s in symbols},
        }
        _runs.move_to_end(run_id)
        while len(_runs) > MAX_RUNS:
            _runs.popitem(last=False)


def _cell(run_id: str | None, symbol: str | None, agent: str | None) -> dict[str, Any] | None:
    run = _runs.get(run_id or _run_id())
    if not run:
        return None
    sym = run["symbols"].get((symbol or symbol_var.get()))
    if sym is None:
        return None
    return sym.get(agent or agent_var.get())


def agent_start(agent: str | None = None, *, run_id: str | None = None, symbol: str | None = None) -> None:
    with _lock:
        cell = _cell(run_id, symbol, agent)
        if cell is not None:
            cell["status"] = "running"
            cell["started_at"] = now_iso()


def agent_finish(agent: str | None = None, *, status: str = "done", score: float | None = None,
                 error: str | None = None, run_id: str | None = None, symbol: str | None = None) -> None:
    with _lock:
        cell = _cell(run_id, symbol, agent)
        if cell is None:
            return
        cell["status"] = status
        cell["finished_at"] = now_iso()
        cell["score"] = score
        cell["error"] = error


def record_tokens(agent: str, input_tokens: int, output_tokens: int, *, provider: str | None = None,
                  model: str | None = None, duration_ms: float | None = None,
                  run_id: str | None = None, symbol: str | None = None) -> None:
    """Accumulate token usage for an agent. Called once per LLM attempt (retries add up, like real cost)."""
    with _lock:
        cell = _cell(run_id, symbol, agent)
        if cell is None:
            return
        cell["input_tokens"] += int(input_tokens or 0)
        cell["output_tokens"] += int(output_tokens or 0)
        if provider:
            cell["provider"] = provider
        if model:
            cell["model"] = model
        if duration_ms is not None:
            cell["duration_ms"] = round((cell["duration_ms"] or 0) + duration_ms, 1)


def finish_run(run_id: str, status: str = "completed") -> dict[str, Any]:
    """Mark a run finished and persist its token totals to kv so they survive a restart."""
    with _lock:
        run = _runs.get(run_id)
        if run:
            run["status"] = status
            run["finished_at"] = now_iso()
        totals = _totals(run) if run else {}
    try:
        from app.store.db import kv_set
        kv_set("run_tokens", run_id, totals)
    except Exception:  # noqa: BLE001 - persistence is best-effort, never break a completed run
        pass
    return totals


def _totals(run: dict[str, Any]) -> dict[str, Any]:
    input_tokens = output_tokens = 0
    by_model: dict[str, dict[str, int]] = {}
    agents_done = agents_total = 0
    for sym in run["symbols"].values():
        for name, cell in sym.items():
            agents_total += 1
            if cell["status"] in ("done", "error", "skipped"):
                agents_done += 1
            input_tokens += cell["input_tokens"]
            output_tokens += cell["output_tokens"]
            key = cell.get("model") or cell.get("provider")
            if key and (cell["input_tokens"] or cell["output_tokens"]):
                m = by_model.setdefault(key, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
                m["input_tokens"] += cell["input_tokens"]
                m["output_tokens"] += cell["output_tokens"]
                m["calls"] += 1
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens, "by_model": by_model,
            "agents_done": agents_done, "agents_total": agents_total}


def snapshot(run_id: str) -> dict[str, Any] | None:
    """A JSON-serialisable view of a run's live progress, or None if the run is not in memory."""
    with _lock:
        run = _runs.get(run_id)
        if not run:
            return None
        return {
            "run_id": run_id, "status": run["status"], "created_at": run["created_at"],
            "finished_at": run["finished_at"], "agents": list(run["agents"]),
            "symbols": {s: {a: dict(cell) for a, cell in agents.items()} for s, agents in run["symbols"].items()},
            "totals": _totals(run),
        }
