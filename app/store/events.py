"""Company event store (announcements, board meetings, corporate actions). Upsert by event_id: no duplicates, history kept."""
from __future__ import annotations

import json
from typing import Any

from app.mcp_server.tools.common import now_iso
from app.store.db import connect


def upsert(events: list[dict[str, Any]]) -> int:
    """Return the number of NEW events (already-known ones only refresh last_seen / status)."""
    new = 0
    with connect() as con:
        for e in events:
            exists = con.execute("SELECT 1 FROM company_events WHERE event_id=?", (e["event_id"],)).fetchone()
            payload = json.dumps(e, default=str)
            if exists:
                con.execute("UPDATE company_events SET payload=?, event_date=?, last_seen=? WHERE event_id=?", (payload, e.get("event_date", ""), now_iso(), e["event_id"]))
            else:
                con.execute("INSERT INTO company_events VALUES (?,?,?,?,?,?,?,?,?)",
                            (e["event_id"], e["symbol"], e["event_type"], e.get("event_date", ""), e.get("announcement_date", ""),
                             e.get("available_at", ""), payload, now_iso(), now_iso()))
                new += 1
    return new


def load(symbol: str, as_of, days: int, ahead_days: int = 90) -> list[dict[str, Any]]:
    """Point-in-time read: only events whose available_at <= as_of; dated within [as_of - days, as_of + ahead_days]."""
    from datetime import timedelta
    lo, hi = (as_of - timedelta(days=days)).date().isoformat(), (as_of + timedelta(days=ahead_days)).date().isoformat()
    with connect() as con:
        rows = con.execute("SELECT payload FROM company_events WHERE symbol=? ORDER BY available_at DESC", (symbol,)).fetchall()
    out = []
    for r in rows:
        e = json.loads(r["payload"])
        day = e.get("event_date") or e.get("announcement_date") or ""
        if e.get("available_at", "") and e["available_at"] <= as_of.isoformat(timespec="seconds") and lo <= day[:10] <= hi:
            out.append(e)
    return out
