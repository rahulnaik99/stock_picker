"""One SQLite file for everything we keep locally (path: DB_PATH, default data/app.db).

Tables: fundamentals_q, news, company_events, fii_dii_daily, oi_snapshots, run_agent_io, runs, kv (small JSON caches), refresh_log.
Tools call `refreshed_today()` before hitting the network, so a dataset refreshed once today is never auto-refreshed again.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from app.mcp_server.tools.common import now_ist
from app.service.settings import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_log (dataset TEXT, key TEXT, refreshed_on TEXT, PRIMARY KEY (dataset, key));
CREATE TABLE IF NOT EXISTS kv (ns TEXT, key TEXT, value TEXT, created_at TEXT, PRIMARY KEY (ns, key));
CREATE TABLE IF NOT EXISTS fundamentals_q (
  symbol TEXT, period_end TEXT, basis TEXT, source TEXT,
  revenue REAL, op_profit REAL, op_kind TEXT, pbt REAL, pat REAL, eps REAL,
  reported_on TEXT, fetched_at TEXT,
  PRIMARY KEY (symbol, period_end, basis, source));
CREATE TABLE IF NOT EXISTS news (
  item_hash TEXT, scope TEXT, title TEXT, publisher TEXT, published_at TEXT, url TEXT, first_seen TEXT,
  PRIMARY KEY (item_hash, scope));
CREATE TABLE IF NOT EXISTS company_events (
  event_id TEXT PRIMARY KEY, symbol TEXT, event_type TEXT, event_date TEXT, announcement_date TEXT,
  available_at TEXT, payload TEXT, first_seen TEXT, last_seen TEXT);
CREATE INDEX IF NOT EXISTS ix_events_symbol ON company_events (symbol, available_at);
CREATE TABLE IF NOT EXISTS fii_dii_daily (
  day TEXT, category TEXT, buy_cr REAL, sell_cr REAL, net_cr REAL, fetched_at TEXT, PRIMARY KEY (day, category));
CREATE TABLE IF NOT EXISTS oi_snapshots (
  symbol TEXT, captured_at TEXT, call_oi REAL, put_oi REAL, pcr REAL, ltp REAL, support TEXT, resistance TEXT, max_pain REAL,
  PRIMARY KEY (symbol, captured_at));
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, as_of TEXT, created_at TEXT, n_stocks INTEGER, scope TEXT);
CREATE TABLE IF NOT EXISTS run_agent_io (
  run_id TEXT, symbol TEXT, agent TEXT, status TEXT, score REAL, confidence REAL, reason TEXT,
  sources TEXT, context_used TEXT, missing TEXT, extra TEXT, provider TEXT, model TEXT, note TEXT, created_at TEXT,
  PRIMARY KEY (run_id, symbol, agent));
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    s = get_settings()
    path = s.path(s.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ---- refresh gate: "refreshed once today -> do not auto-refresh again" -------------------------
def today() -> str:
    return now_ist().date().isoformat()


def refreshed_today(dataset: str, key: str) -> bool:
    if get_settings().force_refresh:          # --force-refresh / FORCE_REFRESH=1 bypasses the once-a-day gate
        return False
    with connect() as con:
        row = con.execute("SELECT refreshed_on FROM refresh_log WHERE dataset=? AND key=?", (dataset, key)).fetchone()
    return bool(row and row["refreshed_on"] == today())


def mark_refreshed(dataset: str, key: str) -> None:
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO refresh_log VALUES (?,?,?)", (dataset, key, today()))


def reset_daily_caches() -> None:
    """Always-fresh mode: drop the once-a-day refresh gate and the volatile derived caches (option-chain snapshot,
    shared industry score) so the next run refetches every tool live. Stored history (results, FII/DII, OI snapshots,
    events) is NOT deleted — it is still needed for multi-day analytics and is overwritten/appended on the fresh pull."""
    with connect() as con:
        con.execute("DELETE FROM refresh_log")
        con.execute("DELETE FROM kv WHERE ns IN ('option_chain', 'industry_score')")


# ---- small JSON cache ----------------------------------------------------------------------------
def kv_get(ns: str, key: str) -> Any | None:
    with connect() as con:
        row = con.execute("SELECT value FROM kv WHERE ns=? AND key=?", (ns, key)).fetchone()
    return json.loads(row["value"]) if row else None


def kv_set(ns: str, key: str, value: Any) -> None:
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?)", (ns, key, json.dumps(value, default=str), now_ist().isoformat(timespec="seconds")))
