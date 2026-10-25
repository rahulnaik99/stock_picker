"""News via Google News RSS (free, no key). Headlines are leads, not verified facts; every item keeps publisher + URL + time."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from app.tools.common import IST, envelope, http_get, parse_as_of, unavailable

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


def _search(query: str, days: int, cutoff, limit: int = 15) -> list[dict]:
    root = ET.fromstring(http_get(GNEWS.format(q=quote(f"{query} when:{days}d")), as_json=False))
    seen, out = set(), []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        key = re.sub(r"\W+", " ", title.lower())[:80]  # de-duplicate syndicated copies
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "")
            dt = dt if dt.tzinfo else dt.replace(tzinfo=IST)
        except (TypeError, ValueError):
            continue
        if key in seen or dt > cutoff or dt < cutoff - timedelta(days=days):
            continue
        seen.add(key)
        out.append({"title": title, "publisher": (it.findtext("source") or "").strip(), "url": (it.findtext("link") or "").strip(),
                    "published_at": dt.isoformat()})
    out.sort(key=lambda x: x["published_at"], reverse=True)
    return out[:limit]


def get_company_news(symbol: str, company_name: str = "", days: int = 7, as_of: str = "") -> dict:
    """Recent headlines for one company (published before as_of)."""
    q = f'"{company_name or symbol}" (NSE OR BSE OR shares OR results OR RBI)'
    try:
        items = _search(q, days, parse_as_of(as_of))
    except Exception as exc:
        return unavailable("Google News RSS", str(exc), as_of)
    return envelope("ok" if items else "partial", "Google News RSS", items, None if items else "no headlines in window", as_of)


def get_sector_news(query: str, days: int = 14, as_of: str = "") -> dict:
    """Headlines for a sector/macro query, e.g. 'RBI repo rate banks' or 'NBFC asset quality India'."""
    try:
        items = _search(query, days, parse_as_of(as_of))
    except Exception as exc:
        return unavailable("Google News RSS", str(exc), as_of)
    return envelope("ok" if items else "partial", "Google News RSS", items, None if items else "no headlines in window", as_of)
