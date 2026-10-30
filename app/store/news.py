"""News store: insert only items not seen before (dedupe by normalised title per scope)."""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any

from app.mcp_server.tools.common import now_iso, parse_any_dt
from app.store.db import connect


def item_hash(title: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", " ", title.lower()).strip()[:100].encode()).hexdigest()[:16]


def upsert(scope: str, items: list[dict[str, Any]]) -> int:
    """Return the number of NEW items."""
    new = 0
    with connect() as con:
        for it in items:
            cur = con.execute("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?)",
                              (item_hash(it["title"]), scope, it["title"], it.get("publisher"), it.get("published_at"), it.get("url"), now_iso()))
            new += cur.rowcount
    return new


def load(scope: str, as_of, days: int, limit: int = 12) -> list[dict[str, Any]]:
    start = (as_of - timedelta(days=days)).isoformat()
    with connect() as con:
        rows = con.execute("SELECT title, publisher, published_at, url FROM news WHERE scope=? AND published_at<=? AND published_at>=? "
                           "ORDER BY published_at DESC LIMIT ?", (scope, as_of.isoformat(), start, limit * 3)).fetchall()
    # ISO strings with different offsets do not sort reliably: re-sort on parsed datetimes
    items = [dict(r) for r in rows]
    items.sort(key=lambda x: parse_any_dt(x["published_at"]) or as_of.replace(year=1970), reverse=True)
    return items[:limit]
