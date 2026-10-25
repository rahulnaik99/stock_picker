"""Save and reload every run: per stock, per agent: source, reason, score, context used. (Basis for a later UI.)

Rows live in SQLite table `run_agent_io`. Agent "final" holds the weighted result, "decision_maker" the LLM reconciliation.
"""
from __future__ import annotations

import json
from typing import Any

from app.mcp_server.tools.common import now_iso
from app.store.db import connect


def _j(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def save_run(run_id: str, as_of: str, scope: str, results: list[Any]) -> int:
    """results: list[StockResult]. Returns rows written."""
    stamp, n = now_iso(), 0
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)", (run_id, as_of, stamp, len(results), scope))
        con.execute("DELETE FROM run_agent_io WHERE run_id=?", (run_id,))
        for s in results:
            tok_in = tok_out = 0
            for name, r in s.agent_results.items():
                tok_in += getattr(r, "input_tokens", 0) or 0
                tok_out += getattr(r, "output_tokens", 0) or 0
                con.execute("INSERT OR REPLACE INTO run_agent_io VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (run_id, s.symbol, name, r.status, r.score, r.confidence, r.reasoning, _j(r.sources), r.context_used,
                             _j(r.missing_data),
                             _j({"evidence": r.evidence, "input_tokens": getattr(r, "input_tokens", 0),
                                 "output_tokens": getattr(r, "output_tokens", 0), **r.extra}),
                             r.provider, r.model, r.note, stamp))
                n += 1
            if s.decision is not None:
                d = s.decision
                tok_in += getattr(d, "input_tokens", 0) or 0
                tok_out += getattr(d, "output_tokens", 0) or 0
                con.execute("INSERT OR REPLACE INTO run_agent_io VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (run_id, s.symbol, "decision_maker", "ok", d.proposed_score, d.confidence, d.reasoning, "[]",
                             _j({"specialist_scores": s.scores, "agent_status": s.agent_status}), "[]",
                             _j({"agreements": d.agreements, "conflicts": d.conflicts, "risks": d.risks, "next_catalyst": d.next_catalyst,
                                 "invalidation": d.invalidation, "suggested_trade_status": d.trade_status,
                                 "input_tokens": getattr(d, "input_tokens", 0), "output_tokens": getattr(d, "output_tokens", 0)}),
                             getattr(d, "provider", "") or "", getattr(d, "model", "") or "", "", stamp))
                n += 1
            con.execute("INSERT OR REPLACE INTO run_agent_io VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, s.symbol, "final", "ok" if s.valid else "invalid", s.final_score, s.confidence, s.reasoning_summary, "[]",
                         _j({"weights_version": s.weights_version, "scores": s.scores}), _j(s.risk_flags),
                         _j({"rank": getattr(s, "rank", None), "direction": s.direction, "consensus": s.consensus_status,
                             "trade_status": s.trade_status, "data_status": s.data_status, "completeness": s.data_completeness,
                             "adjustments": s.status_adjustments, "error": s.error,
                             "input_tokens": tok_in, "output_tokens": tok_out, "total_tokens": tok_in + tok_out}),
                         "", "", "", stamp))
            n += 1
    return n


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]


def load_run(run_id: str, symbol: str | None = None) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM run_agent_io WHERE run_id=?", [run_id]
    if symbol:
        q, args = q + " AND symbol=?", args + [symbol.upper()]
    with connect() as con:
        rows = [dict(r) for r in con.execute(q + " ORDER BY symbol, agent", args)]
    for r in rows:
        for k in ("sources", "missing", "extra"):
            try:
                r[k] = json.loads(r[k]) if r[k] else None
            except ValueError:
                pass
    return rows
