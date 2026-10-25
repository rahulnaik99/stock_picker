"""RBI policy: official press-release RSS (+ optional manual override file for exact policy numbers).

Why a manual file: the RSS carries headlines, not always the repo-rate figure. The auto-detected
rate is used when found in release text; otherwise config/manual_macro.json (which YOU keep
current from the MPC statement) is used, and the output says which one it was.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from app.mcp_server.providers.common import ProviderError, Unavailable, http_get, ttl_cache
from app.service.settings import get_settings

FEEDS = {"press_releases": "https://rbi.org.in/pressreleases_rss.xml",
         "notifications": "https://rbi.org.in/notifications_rss.xml"}
KEYWORDS = ("repo", "monetary policy", "mpc", "crr", "slr", "liquidity", "policy rate", "banking", "nbfc",
            "penalty", "lending", "deposit", "credit", "basel", "capital", "priority sector", "digital lending")
REPO_RE = re.compile(r"(?:policy\s+)?repo\s+rate[^.]{0,160}?(\d{1,2}\.\d{1,2})\s*(?:per\s*cent|%)", re.I)


def _manual() -> dict:
    p = get_settings().path(get_settings().manual_macro_file)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@ttl_cache(1800)
def policy(as_of_iso: str, days: int = 45) -> dict:
    as_of = datetime.fromisoformat(as_of_iso)
    items, errors = [], []
    for name, url in FEEDS.items():
        try:
            feed = feedparser.parse(http_get(url))
        except ProviderError as e:
            errors.append(f"{name}: {e}")
            continue
        for e in feed.entries:
            title = e.get("title", "")
            blob = f"{title} {e.get('summary', '')}"
            if not any(k in blob.lower() for k in KEYWORDS):
                continue
            try:
                pub = parsedate_to_datetime(e["published"]) if e.get("published") else None
            except (TypeError, ValueError):
                pub = None
            if pub and pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            if pub and (pub > as_of or (as_of - pub).days > days):
                continue
            items.append({"title": title, "url": e.get("link"), "published_at": pub.isoformat() if pub else None, "feed": name})
    if not items and errors:
        raise ProviderError("; ".join(errors))
    items.sort(key=lambda x: x["published_at"] or "", reverse=True)

    repo, repo_src = None, None
    for it in items:
        m = REPO_RE.search(it["title"])
        if m:
            repo, repo_src = float(m.group(1)), it["url"]
            break
    manual = _manual()
    if repo is None and manual.get("repo_rate") is not None:
        repo, repo_src = manual["repo_rate"], f"manual_macro.json ({manual.get('as_of', 'date not set')}; {manual.get('source', '')})"
    return {
        "repo_rate_pct": repo, "repo_rate_source": repo_src,
        "other_policy_rates_manual": {k: manual.get(k) for k in ("crr", "slr", "sdf", "msf", "stance") if manual.get(k) is not None},
        "recent_releases": items[:20], "feed_errors": errors,
        "note": "Effective date differs from announcement date; market impact is uncertain.",
    }
