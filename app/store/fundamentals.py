"""Quarterly results store + deterministic metrics (QoQ / YoY / streaks). All maths here is plain Python, not LLM.

Units: INR crore. Sources: "import" (your own CSV), "nse" (financial-results filings), "yahoo".
A series never mixes sources: `pick_series` returns one source (preferring import > nse > yahoo, needing >= MIN_QUARTERS).
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.mcp_server.tools.common import now_iso
from app.store.db import connect

FIELDS = ("revenue", "op_profit", "pbt", "pat", "eps")
SOURCE_PRIORITY = ("import", "nse", "yahoo")
MIN_QUARTERS = 6
MAX_QUARTERS = 16


def upsert_quarters(rows: list[dict[str, Any]]) -> int:
    n = 0
    with connect() as con:
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO fundamentals_q (symbol, period_end, basis, source, revenue, op_profit, op_kind, pbt, pat, eps, reported_on, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["symbol"], r["period_end"], r.get("basis", "consolidated"), r["source"], r.get("revenue"), r.get("op_profit"),
                 r.get("op_kind"), r.get("pbt"), r.get("pat"), r.get("eps"), r.get("reported_on"), now_iso()))
            n += 1
    return n


def latest_period_end(symbol: str, as_of: date) -> date | None:
    with connect() as con:
        row = con.execute("SELECT MAX(period_end) m FROM fundamentals_q WHERE symbol=? AND period_end<=?", (symbol, as_of.isoformat())).fetchone()
    return date.fromisoformat(row["m"]) if row and row["m"] else None


def pick_series(symbol: str, as_of: date) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Return (rows oldest->newest, source, basis). Consolidated preferred; one source only."""
    with connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM fundamentals_q WHERE symbol=? AND period_end<=? ORDER BY period_end", (symbol, as_of.isoformat()))]
    if not rows:
        return [], None, None
    best: tuple[int, list[dict[str, Any]], str, str] | None = None
    for src in SOURCE_PRIORITY:
        for basis in ("consolidated", "standalone"):
            sel = [r for r in rows if r["source"] == src and r["basis"] == basis]
            if sel and (best is None or (len(sel) >= MIN_QUARTERS > best[0]) or (len(sel) > best[0] and best[0] < MIN_QUARTERS)):
                best = (len(sel), sel, src, basis)
                if len(sel) >= MIN_QUARTERS:
                    return sel[-MAX_QUARTERS:], src, basis
    assert best is not None
    return best[1][-MAX_QUARTERS:], best[2], best[3]


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:   # growth from zero/negative is not meaningful
        return None
    return round((new / old - 1) * 100, 2)


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """rows oldest->newest. For each metric: last 6 QoQ changes (qoq_1 = latest quarter vs the one before), last 4 YoY,
    consecutive up/down streak, and latest margin trend."""
    out: dict[str, Any] = {"quarters_available": len(rows), "history_short": len(rows) < MAX_QUARTERS, "gaps": _gaps(rows), "metrics": {}}
    for f in FIELDS:
        vals = [r.get(f) for r in rows]
        if sum(v is not None for v in vals) < 2:
            continue
        qoq = {f"qoq_{k}": _pct(vals[-k], vals[-k - 1]) for k in range(1, 7) if len(vals) > k}
        yoy = {f"yoy_{k}": _pct(vals[-k], vals[-k - 4]) for k in range(1, 5) if len(vals) > k + 3}
        streak = 0
        for i in range(len(vals) - 1, 0, -1):
            if vals[i] is None or vals[i - 1] is None or vals[i] == vals[i - 1]:
                break
            d = 1 if vals[i] > vals[i - 1] else -1
            if streak and (d > 0) != (streak > 0):
                break
            streak += d
        out["metrics"][f] = {"latest": vals[-1], "latest_period": rows[-1]["period_end"], **qoq, **yoy, "streak_quarters": streak,
                             "kind": rows[-1].get("op_kind") if f == "op_profit" else None}
    rev, pat = [r.get("revenue") for r in rows], [r.get("pat") for r in rows]
    if len(rows) >= 5 and rev[-1] and rev[-5] and pat[-1] is not None and pat[-5] is not None:
        out["net_margin_pct"] = {"latest": round(pat[-1] / rev[-1] * 100, 2), "year_ago": round(pat[-5] / rev[-5] * 100, 2)}
    return out


def _gaps(rows: list[dict[str, Any]]) -> list[str]:
    gaps = []
    for a, b in zip(rows, rows[1:]):
        days = (date.fromisoformat(b["period_end"]) - date.fromisoformat(a["period_end"])).days
        if days > 100:
            gaps.append(f"missing quarter(s) between {a['period_end']} and {b['period_end']}")
    return gaps


def import_csv(path: str | Path, source: str = "import") -> int:
    """Load your own history (e.g. a Screener/Excel export saved as CSV). Columns: symbol, period_end (YYYY-MM-DD), revenue,
    op_profit, pbt, pat, eps; optional: basis, op_kind. Values in INR crore."""
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            num = lambda k: float(r[k]) if r.get(k) not in (None, "", "-") else None  # noqa: E731
            rows.append({"symbol": r["symbol"].strip().upper(), "period_end": datetime.fromisoformat(r["period_end"]).date().isoformat(),
                         "basis": (r.get("basis") or "consolidated").lower(), "source": source, "op_kind": r.get("op_kind") or None,
                         **{f: num(f) for f in FIELDS}})
    return upsert_quarters(rows)
