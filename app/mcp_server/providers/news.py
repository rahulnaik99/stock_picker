"""News via Google News RSS (free, no key). Headlines are NOT verified facts: every item keeps
publisher, URL and publication time, and syndicated duplicates are removed."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser

from app.mcp_server.providers.common import ProviderError, Unavailable, http_get, ttl_cache

RSS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"\s+-\s+[^-]+$", "", title.lower())).strip()


@ttl_cache(600)
def search(query: str, as_of_iso: str, days: int = 14, limit: int = 15) -> list[dict]:
    as_of = datetime.fromisoformat(as_of_iso)
    text = http_get(RSS.format(q=quote_plus(f"{query} when:{days}d")))
    feed = feedparser.parse(text)
    if not feed.entries:
        raise Unavailable(f"no news found for query: {query}")
    items, seen = [], set()
    for e in feed.entries:
        try:
            pub = parsedate_to_datetime(e.get("published", "")) if e.get("published") else None
        except (TypeError, ValueError):
            pub = None
        if pub and pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if pub and pub > as_of:        # point-in-time
            continue
        key = _norm(e.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        items.append({"title": e.get("title"), "publisher": (e.get("source") or {}).get("title"),
                      "url": e.get("link"), "published_at": pub.isoformat() if pub else None})
    items.sort(key=lambda x: x["published_at"] or "", reverse=True)
    if not items:
        raise Unavailable(f"no news before as_of for query: {query}")
    return items[:limit]
