"""Premium financial news from stock.indianapi.in (Indian Stock Market API).

RATE LIMIT IS TINY: 500 requests/month (~16/day), 1 request/second. So this tool is
MARKET-WIDE and fetched AT MOST ONCE PER DAY — never per-stock, never per-agent. The single
daily pull is stored in the shared news table and every agent/stock in the universe reads the
same cached rows. Financial-sector relevance is applied as a keyword filter before storage.

Auth: header `x-api-key`. Base: https://stock.indianapi.in. News endpoint: /news (JSON array).
If the key is unset or the endpoint is unreachable, the tool returns `unavailable` and the
pipeline simply leans on the free Google-News source — it never crashes.
"""
from __future__ import annotations

from typing import Any

import requests

from app.mcp_server.tools.common import IST, UA, error, http_get, now_ist, ok, parse_any_dt, parse_as_of, unavailable
from app.service.settings import get_settings
from app.store import news as news_store
from app.store.db import mark_refreshed, refreshed_today

BASE = "https://stock.indianapi.in"
NEWS_PATH = "/news"
SCOPE = "indianapi:financial"      # shared SQLite scope for the one daily market-wide pull

# Keep only financial-sector-relevant headlines (this is a financials-only scoring universe).
_FIN_KEYWORDS = (
    "bank", "nbfc", "finance", "financial", "lending", "loan", "credit", "deposit", "nim",
    "npa", "gnpa", "casa", "rbi", "repo", "sebi", "irdai", "insurance", "insurer", "amc",
    "mutual fund", "housing finance", "hfc", "fii", "dii", "fpi", "qip", "rights issue",
    "pledge", "promoter", "results", "q1", "q2", "q3", "q4", "pat", "profit", "dividend",
    "rating", "upgrade", "downgrade", "yield", "bond", "liquidity", "capital", "basel",
    "fintech", "payments", "upi", "brokerage", "exchange", "depository",
)


def _is_financial(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _FIN_KEYWORDS)


def _normalise(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map IndianAPI's news item (schema varies by version) into our standard news row."""
    if not isinstance(item, dict):
        return None
    title = (item.get("title") or item.get("headline") or item.get("heading") or "").strip()
    if not title:
        return None
    url = item.get("url") or item.get("link") or item.get("source_url")
    publisher = item.get("source") or item.get("publisher") or item.get("provider")
    raw_date = item.get("pub_date") or item.get("date") or item.get("published_at") or item.get("datetime") or item.get("time")
    dt = parse_any_dt(raw_date)
    published = (dt.astimezone(IST).isoformat(timespec="seconds") if dt else now_ist().isoformat(timespec="seconds"))
    summary = (item.get("summary") or item.get("description") or item.get("content") or "")[:500]
    return {"title": title, "publisher": publisher, "published_at": published, "url": url, "summary": summary}


def _fetch() -> list[dict[str, Any]]:
    s = get_settings()
    if not s.indianapi_key:
        raise RuntimeError("INDIANAPI_KEY not set")
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "x-api-key": s.indianapi_key, "Accept": "application/json"})
    resp = http_get(sess, f"{BASE}{NEWS_PATH}")
    if resp.status_code in (401, 403):
        raise RuntimeError(f"auth failed (HTTP {resp.status_code}) — check INDIANAPI_KEY")
    if resp.status_code == 429:
        raise RuntimeError("rate limit hit (500/month, 1/sec) — try later")
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}")
    payload = resp.json()
    # Accept either a bare list or {"news": [...]} / {"data": [...]} wrappers
    rows = payload if isinstance(payload, list) else (payload.get("news") or payload.get("data") or [])
    out: list[dict[str, Any]] = []
    for it in rows:
        row = _normalise(it)
        if row and _is_financial(f"{row['title']} {row.get('summary','')}"):
            out.append(row)
    return out


def indianapi_news(as_of: str | None = None, days: int = 7, max_items: int = 20) -> dict[str, Any]:
    """MARKET-WIDE premium financial news from stock.indianapi.in, financial-sector filtered.
    Fetched at most ONCE PER DAY (500 req/month budget) and shared across every stock/agent.
    Returns newest-first items on or before `as_of`; falls back to stored rows if the fetch fails."""
    src = "indianapi.in (SQLite cache, financial-filtered)"
    try:
        t = parse_as_of(as_of)
        live = t.date() >= now_ist().date()
        cache, new, fetch_error = "hit", 0, None
        if live and get_settings().indianapi_key and not refreshed_today("indianapi_news", "market"):
            cache = "refreshed"
            try:
                new = news_store.upsert(SCOPE, _fetch())
                mark_refreshed("indianapi_news", "market")
            except Exception as exc:  # noqa: BLE001 - serve whatever is already stored
                fetch_error, cache = f"{type(exc).__name__}: {str(exc)[:160]}", "stale"
        items = news_store.load(SCOPE, t, days, max_items)
        if not items:
            if not get_settings().indianapi_key:
                return unavailable(src, "INDIANAPI_KEY not set — using free Google-News source instead")
            return (error(src, fetch_error) if fetch_error
                    else unavailable(src, f"no financial news stored in the last {days} days"))
        return ok(src, items, scope="market_wide_financial", cache=cache, new_items=new, fetch_error=fetch_error,
                  note="Premium financial-sector headlines (1 shared daily pull); leads, not verified facts.")
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)
