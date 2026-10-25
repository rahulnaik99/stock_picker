"""NSE public-website endpoints (unofficial JSON APIs): announcements, board meetings, corporate actions, FII/DII, deals.

These endpoints need browser-like headers + a warm-up cookie and can change or block automated access.
Every call goes through retry + cache; failures surface as status="unavailable" (never fabricated data).
Field names below follow NSE's current JSON; verify against a live response on first run (see README).
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime

import requests

from app.ledger.events import make_event_id
from app.tools.common import IST, TTLCache, http_get, log, now_iso, parse_as_of

BASE = "https://www.nseindia.com"
_cache = TTLCache(ttl=600)
_lock = threading.Lock()
_session: requests.Session | None = None
_session_at = 0.0

_HEADERS = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9", "Referer": BASE + "/"}


def _sess() -> requests.Session:
    global _session, _session_at
    with _lock:
        if _session is None or time.time() - _session_at > 900:
            s = requests.Session()
            http_get(BASE, session=s, headers=_HEADERS, as_json=False, retries=1)  # sets cookies
            _session, _session_at = s, time.time()
        return _session


def nse_get(path: str, **params):
    key = (path, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    data = http_get(BASE + path, params=params, headers=_HEADERS, session=_sess())
    _cache.set(key, data)
    return data


def parse_nse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


_TYPES = [
    ("result", r"financial result|quarterly result|audited|unaudited|results"), ("dividend", r"dividend"),
    ("buyback", r"buy[- ]?back"), ("split", r"split|sub-?division"), ("bonus", r"bonus"), ("rights_issue", r"rights"),
    ("merger_acquisition", r"merger|amalgamation|acquisition|scheme of arrangement|demerger"),
    ("fundraise", r"qip|ncd|debenture|fund ?raising|preferential|bond"), ("rating", r"credit rating|rating"),
    ("management_change", r"appointment|resignation|cessation|director|ceo|md &"), ("board_meeting", r"board meeting"),
    ("regulatory_action", r"penalty|sebi|rbi|show cause|fine"),
]


def classify(text: str) -> str:
    t = (text or "").lower()
    for name, pat in _TYPES:
        if re.search(pat, t):
            return name
    return "other"


def _event(symbol, etype, summary, url, published, event_date, status, source_key) -> dict:
    retrieved = now_iso()
    pub = published.isoformat() if published else ""
    ev_date = event_date.date().isoformat() if event_date else ""
    ann_date = published.date().isoformat() if published else ""
    return {
        "event_id": make_event_id(symbol, etype, source_key, ann_date or ev_date), "symbol": symbol, "exchange": "NSE",
        "event_type": etype, "announcement_date": ann_date, "event_date": ev_date, "event_status": status,
        "event_summary": (summary or "")[:400], "source_url": url or "", "published_at": pub, "retrieved_at": retrieved,
        "available_at": pub or retrieved,  # unknown publish time => treated as available only when we first saw it
        "event_impact_score": "", "analyst_reasoning": "",
    }


def fetch_symbol_events(symbol: str, as_of: str = "", lookback_days: int = 180) -> tuple[list[dict], list[str]]:
    """Return (events, errors). Partial failures are reported, not hidden."""
    cutoff = parse_as_of(as_of)
    start = cutoff.timestamp() - lookback_days * 86400
    events, errors = [], []

    def run(name, fn):
        try:
            events.extend(fn())
        except Exception as exc:  # one endpoint failing must not hide the others
            log.warning("NSE %s failed for %s: %s", name, symbol, exc)
            errors.append(f"{name}: {exc}")

    def announcements():
        out = []
        for r in nse_get("/api/corporate-announcements", index="equities", symbol=symbol) or []:
            dt = parse_nse_dt(r.get("an_dt") or r.get("sort_date"))
            text = f"{r.get('desc', '')}: {r.get('attchmntText', '')}"
            etype = classify(text)
            out.append(_event(symbol, etype, text, r.get("attchmntFile"), dt, dt, "announced", str(r.get("seq_id") or r.get("attchmntFile") or text[:60])))
        return out

    def board_meetings():
        out = []
        for r in nse_get("/api/corporate-board-meetings", index="equities", symbol=symbol) or []:
            when = parse_nse_dt(r.get("bm_date"))
            intimated = parse_nse_dt(r.get("bm_timestamp"))
            text = f"Board meeting: {r.get('bm_purpose', '')} {r.get('bm_desc', '')}"
            status = "scheduled" if when and when > cutoff else "held"
            out.append(_event(symbol, "board_meeting", text, r.get("attachment"), intimated, when, status, f"bm:{r.get('bm_date')}:{r.get('bm_purpose')}"))
        return out

    def actions():
        out = []
        for r in nse_get("/api/corporates-corporateActions", index="equities", symbol=symbol) or []:
            ex = parse_nse_dt(r.get("exDate"))
            text = r.get("subject", "")
            out.append(_event(symbol, classify(text) if classify(text) != "other" else "corporate_action", text, "", None, ex, "scheduled" if ex and ex > cutoff else "executed", f"ca:{r.get('exDate')}:{text[:60]}"))
        return out

    for name, fn in (("announcements", announcements), ("board_meetings", board_meetings), ("corporate_actions", actions)):
        run(name, fn)
    # keep only what was knowable at `cutoff` and is inside the lookback window (upcoming scheduled items stay)
    keep = []
    for e in events:
        avail = datetime.fromisoformat(e["available_at"])
        if avail.tzinfo is None:
            avail = avail.replace(tzinfo=IST)
        if avail <= cutoff and (avail.timestamp() >= start or e["event_status"] == "scheduled"):
            keep.append(e)
    keep.sort(key=lambda e: e["available_at"], reverse=True)
    return keep, errors
