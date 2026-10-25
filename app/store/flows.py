"""FII / DII daily flows: stored in SQLite (one row per day per category) and analysed in plain Python.

NSE only shows the latest session, so history builds up one day per run. Re-running the same day updates the same row
(no double counting). To backfill, load a CSV with `import_csv` (columns: date, category, buy_cr, sell_cr, net_cr).
Everything here is MARKET-WIDE. It says nothing about one stock.
"""
from __future__ import annotations

import csv
from datetime import date
from typing import Any

from app.mcp_server.tools.common import now_iso, parse_any_dt
from app.store.db import connect

MIN_DAYS_FOR_WINDOW = {"net_5d": 5, "net_20d": 20}


def _num(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def category(raw: str | None) -> str | None:
    t = (raw or "").upper()
    if "FII" in t or "FPI" in t:
        return "FII"
    if "DII" in t:
        return "DII"
    return None


def upsert_rows(rows: list[dict[str, Any]]) -> int:
    """rows: NSE style {category, date, buy_value_cr, sell_value_cr, net_value_cr}. Returns rows written."""
    n = 0
    with connect() as con:
        for r in rows:
            cat, day = category(r.get("category")), parse_any_dt(r.get("date"))
            if not cat or not day:
                continue
            buy, sell, net = _num(r.get("buy_value_cr")), _num(r.get("sell_value_cr")), _num(r.get("net_value_cr"))
            if net is None and buy is not None and sell is not None:
                net = round(buy - sell, 2)
            if net is None:
                continue
            con.execute("INSERT OR REPLACE INTO fii_dii_daily VALUES (?,?,?,?,?,?)", (day.date().isoformat(), cat, buy, sell, net, now_iso()))
            n += 1
    return n


def import_csv(path: str) -> int:
    with open(path, newline="") as fh:
        rows = [{"category": r["category"], "date": r["date"], "buy_value_cr": r.get("buy_cr"), "sell_value_cr": r.get("sell_cr"),
                 "net_value_cr": r.get("net_cr")} for r in csv.DictReader(fh)]
    return upsert_rows(rows)


def load_series(as_of: date, cat: str, limit: int = 60) -> list[tuple[str, float]]:
    """(day, net_cr) oldest -> newest, only days up to as_of."""
    with connect() as con:
        rows = con.execute("SELECT day, net_cr FROM fii_dii_daily WHERE category=? AND day<=? ORDER BY day DESC LIMIT ?",
                           (cat, as_of.isoformat(), limit)).fetchall()
    return [(r["day"], float(r["net_cr"])) for r in reversed(rows)]


def analyse(series: list[tuple[str, float]]) -> dict[str, Any]:
    """Pure function. Windows with too little history are reported as 'insufficient_history', never as a partial sum."""
    if not series:
        return {"days_available": 0}
    nets = [v for _, v in series]
    out: dict[str, Any] = {"days_available": len(nets), "latest_day": series[-1][0], "net_1d": round(nets[-1], 2)}
    for key, n in MIN_DAYS_FOR_WINDOW.items():
        out[key] = round(sum(nets[-n:]), 2) if len(nets) >= n else "insufficient_history"
    streak = 0
    for v in reversed(nets):
        sign = 1 if v > 0 else -1 if v < 0 else 0
        if sign == 0 or (streak and (sign > 0) != (streak > 0)):
            break
        streak += sign
    out["streak_days"] = streak                      # +N = N consecutive net-buy days, -N = net-sell days
    if len(nets) >= 5:
        avg5 = sum(nets[-5:]) / 5
        out["avg_5d"] = round(avg5, 2)
        if len(nets) >= 20:
            avg20 = sum(nets[-20:]) / 20
            out["avg_20d"] = round(avg20, 2)
            out["momentum"] = "improving" if avg5 > avg20 else "worsening" if avg5 < avg20 else "flat"
        pos = sum(v > 0 for v in nets[-5:])
        out["direction_5d"] = "net buying" if pos >= 4 else "net selling" if pos <= 1 else "mixed"
    return out


def analytics(as_of: date) -> dict[str, Any]:
    return {cat: analyse(load_series(as_of, cat)) for cat in ("FII", "DII")}
