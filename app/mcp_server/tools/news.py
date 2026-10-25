"""News via Google News RSS (free), kept in SQLite so history builds up and only NEW headlines are inserted.

Headlines are leads, not verified facts: each item keeps publisher, URL and time (stored in IST so point-in-time reads are exact).
Refresh rule: a query is fetched at most once per day (the first load); later calls read the stored items.
"""
from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import feedparser
import requests

from app.mcp_server.tools.common import IST, UA, error, http_get, now_ist, ok, parse_as_of, unavailable
from app.store import news as news_store
from app.store.db import mark_refreshed, refreshed_today


def _fetch(query: str, days: int) -> list[dict[str, Any]]:
    url = f"https://news.google.com/rss/search?q={quote_plus(f'{query} when:{max(1, days)}d')}&hl=en-IN&gl=IN&ceid=IN:en"
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    resp = http_get(sess, url)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}")
    items = []
    for e in feedparser.parse(resp.content).entries:
        title = e.get("title", "").strip()
        try:
            pub = parsedate_to_datetime(e.get("published", "")).astimezone(IST)
        except (TypeError, ValueError):
            continue  # no usable timestamp: cannot prove point-in-time
        if title:
            publisher = (e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else None
            items.append({"title": title, "publisher": publisher, "published_at": pub.isoformat(timespec="seconds"), "url": e.get("link")})
    return items


def news_search(query: str, days: int = 7, as_of: str | None = None, max_items: int = 12) -> dict[str, Any]:
    """Recent news for a query (company name/ticker, or a sector/macro topic). Stored locally, deduplicated, newest first,
    only items published on or before `as_of`."""
    src = "google_news_rss (SQLite cache)"
    try:
        t = parse_as_of(as_of)
        scope = " ".join(query.lower().split())
        live = t.date() >= now_ist().date()
        cache, new, fetch_error = "hit", 0, None
        if live and not refreshed_today("news", scope):
            cache = "refreshed"
            try:
                new = news_store.upsert(scope, _fetch(query, max(days, 7)))
                mark_refreshed("news", scope)
            except Exception as exc:  # noqa: BLE001 - fall back to what is already stored
                fetch_error, cache = f"{type(exc).__name__}: {str(exc)[:160]}", "stale"
        items = news_store.load(scope, t, days, max_items)
        if not items:
            return (error(src, fetch_error) if fetch_error else unavailable(src, f"no news stored for '{query}' in the last {days} days"))
        return ok(src, items, query=query, cache=cache, new_items=new, fetch_error=fetch_error, note="Headlines only; not verified facts.")
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)
