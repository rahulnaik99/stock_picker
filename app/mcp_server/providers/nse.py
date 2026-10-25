"""NSE India public JSON endpoints: announcements, board meetings, corporate actions, results,
FII/DII, bulk/block deals, shareholding.

These are the same endpoints nseindia.com's own pages call. They are UNOFFICIAL (no SLA, cookie
handshake, anti-bot rules). We use one warmed-up session, space requests out, cache, and report
failures explicitly. Respect NSE's terms of use; do not hammer the site.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.mcp_server.providers.common import (IST, ProviderError, Unavailable, first, now_iso, ttl_cache, UA)
from app.service.logger import get_logger
from app.service.settings import get_settings

log = get_logger(__name__)
BASE = "https://www.nseindia.com"
_lock = threading.Lock()
_client: httpx.Client | None = None
_last_call = 0.0
MIN_GAP = 0.5  # seconds between NSE requests


def _session(force: bool = False) -> httpx.Client:
    global _client
    if _client is None or force:
        s = get_settings()
        _client = httpx.Client(timeout=s.http_timeout, follow_redirects=True, headers={
            "User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE + "/"})
        try:
            _client.get(BASE + "/")  # cookie handshake
        except httpx.HTTPError as e:
            raise ProviderError(f"NSE handshake failed: {e}") from e
    return _client


def get_json(path: str, params: dict | None = None) -> Any:
    global _last_call
    s = get_settings()
    last: Exception | None = None
    for attempt in range(max(1, s.http_retries)):
        with _lock:
            wait = MIN_GAP - (time.monotonic() - _last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                r = _session(force=attempt > 0 and "403" in str(last or "")).get(BASE + path, params=params)
            except (httpx.HTTPError, ProviderError) as e:
                last = e
                _last_call = time.monotonic()
                time.sleep(0.8 * 2 ** attempt)
                continue
            _last_call = time.monotonic()
        if r.status_code in (401, 403):
            last = ProviderError(f"NSE HTTP {r.status_code} (blocked / cookie expired)")
            time.sleep(1.0)
            continue
        if r.status_code >= 400:
            last = ProviderError(f"NSE HTTP {r.status_code} for {path}")
            time.sleep(0.8 * 2 ** attempt)
            continue
        try:
            return r.json()
        except ValueError:
            last = ProviderError(f"NSE returned non-JSON for {path}")
    raise ProviderError(f"NSE {path} failed: {last}")


# ----------------------------------------------------------------------------- date helpers
_DATE_FORMATS = ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d-%b-%Y %H:%M")


def parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), f).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------------- event classification
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("regulatory_action", ("penalty", "show cause", "monetary penalty", "adjudicat", "sebi order", "rbi order")),
    ("buyback", ("buy back", "buyback", "buy-back")),
    ("split", ("stock split", "sub-division", "subdivision")),
    ("bonus", ("bonus",)),
    ("rights_issue", ("rights issue", "rights entitlement")),
    ("merger_acquisition", ("merger", "amalgamation", "acquisition", "scheme of arrangement", "demerger", "takeover")),
    ("dividend", ("dividend",)),
    ("board_meeting", ("board meeting", "meeting of the board", "board of directors meeting")),
    ("results", ("financial result", "results", "quarter ended", "half year ended", "year ended", "audited", "unaudited")),
    ("credit_rating", ("credit rating", "rating")),
    ("fund_raise", ("fund raising", "fund raise", "qip", "debenture", "ncd", "preferential", "raising of funds")),
    ("management_change", ("appointment", "resignation", "cessation", "re-appointment")),
    ("analyst_meet", ("analyst", "investor meet", "earnings call", "conference call", "presentation")),
]


def classify(text: str) -> str:
    t = (text or "").lower()
    for label, kws in _RULES:
        if any(k in t for k in kws):
            return label
    return "other"


def make_event(symbol: str, kind: str, text: str, *, published: datetime | None, event_date: datetime | None,
               url: str, doc_id: str, status: str | None = None) -> dict:
    etype = classify(text)
    today = datetime.now(IST).date()
    if status is None:
        status = "upcoming" if event_date and event_date.date() > today and etype in ("board_meeting", "results", "dividend") else "announced"
    key = f"{symbol}|{etype}|{kind}|{doc_id or text[:80]}|{(event_date or published or '')!s:.10}"
    ts = now_iso()
    return {
        "event_id": hashlib.sha1(key.encode()).hexdigest()[:16], "symbol": symbol, "exchange": "NSE",
        "event_type": etype, "announcement_date": published.date().isoformat() if published else "",
        "event_date": event_date.date().isoformat() if event_date else "", "event_status": status,
        "event_summary": re.sub(r"\s+", " ", text or "").strip()[:500], "source_url": url,
        "published_at": published.isoformat() if published else "", "retrieved_at": ts,
        "available_at": (published.isoformat() if published else ts), "event_impact_score": "",
        "analyst_reasoning": "", "last_updated": ts, "source_kind": kind,
    }


# ----------------------------------------------------------------------------- per-stock events
def _rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "rows", "results"):
            if isinstance(payload.get(k), list):
                return payload[k]
    return []


def announcements(symbol: str) -> list[dict]:
    out = []
    for r in _rows(get_json("/api/corporate-announcements", {"index": "equities", "symbol": symbol})):
        text = f"{first(r, 'desc', 'subject', default='')}. {first(r, 'attchmntText', 'sm_name', default='')}"
        pub = parse_dt(first(r, "an_dt", "sort_date", "broadcastdate"))
        url = first(r, "attchmntFile", "attachment", default="") or f"{BASE}/get-quotes/equity?symbol={symbol}"
        out.append(make_event(symbol, "announcement", text, published=pub, event_date=pub, url=url,
                              doc_id=str(first(r, "seq_id", "an_dt", default=""))))
    return out


def board_meetings(symbol: str) -> list[dict]:
    out = []
    for r in _rows(get_json("/api/corporate-board-meetings", {"index": "equities", "symbol": symbol})):
        text = f"Board meeting: {first(r, 'bm_purpose', 'purpose', default='')}. {first(r, 'bm_desc', 'description', default='')}"
        when = parse_dt(first(r, "bm_date", "meetingDate"))
        pub = parse_dt(first(r, "bm_timestamp", "creation_date"))
        out.append(make_event(symbol, "board_meeting", text, published=pub, event_date=when,
                              url=first(r, "attachment", default="") or f"{BASE}/get-quotes/equity?symbol={symbol}",
                              doc_id=str(first(r, "bm_date", default="")) + str(first(r, "bm_purpose", default="")[:30]),
                              status="upcoming" if when and when.date() >= datetime.now(IST).date() else "held"))
    return out


def corporate_actions(symbol: str) -> list[dict]:
    out = []
    for r in _rows(get_json("/api/corporates-corporateActions", {"index": "equities", "symbol": symbol})):
        text = f"{first(r, 'subject', 'purpose', default='')}"
        ex = parse_dt(first(r, "exDate"))
        out.append(make_event(symbol, "corporate_action", text, published=parse_dt(first(r, "recDate")), event_date=ex,
                              url=f"{BASE}/get-quotes/equity?symbol={symbol}", doc_id=f"{text[:60]}{first(r, 'exDate', default='')}"))
    return out


def financial_results(symbol: str) -> list[dict]:
    out = []
    for r in _rows(get_json("/api/corporates-financial-results", {"index": "equities", "symbol": symbol, "period": "Quarterly"})):
        text = (f"Financial results period ended {first(r, 'toDate', default='?')} "
                f"({first(r, 'audited', 'resultDescription', default='')}); income={first(r, 'income', default='n/a')}, "
                f"profit/loss={first(r, 'profitLossForPeriod', 'reProLossBefTax', default='n/a')}")
        filed = parse_dt(first(r, "filingDate", "broadcast_date"))
        out.append(make_event(symbol, "financial_result", "financial results " + text, published=filed,
                              event_date=parse_dt(first(r, "toDate")), url=first(r, "xbrl", "attachment", default="") or f"{BASE}/get-quotes/equity?symbol={symbol}",
                              doc_id=str(first(r, "toDate", default="")) + str(first(r, "filingDate", default=""))))
    return out


def stock_events(symbol: str, as_of: datetime, limit: int = 40) -> tuple[list[dict], list[str]]:
    """All event kinds merged, deduped, point-in-time filtered. Returns (events, warnings)."""
    events, warnings = [], []
    for fn in (announcements, board_meetings, corporate_actions, financial_results):
        try:
            events += fn(symbol)
        except ProviderError as e:
            warnings.append(f"{fn.__name__}: {e}")
    if not events and warnings:
        raise ProviderError("; ".join(warnings))
    seen, uniq = set(), []
    for e in events:
        if e["event_id"] not in seen:
            seen.add(e["event_id"])
            uniq.append(e)
    # point-in-time: drop anything published after as_of
    uniq = [e for e in uniq if not e["published_at"] or datetime.fromisoformat(e["published_at"]) <= as_of]
    uniq.sort(key=lambda e: e["published_at"] or e["event_date"], reverse=True)
    return uniq[:limit], warnings


# ----------------------------------------------------------------------------- flows
@ttl_cache(900)
def fii_dii() -> list[dict]:
    rows = _rows(get_json("/api/fiidiiTradeReact"))
    if not rows:
        raise Unavailable("NSE FII/DII report returned nothing")
    return rows


def _deals(kind: str, symbol: str, days: int = 90) -> list[dict]:
    end = datetime.now(IST).date()
    path = f"/api/historical/{kind}-deals"
    return _rows(get_json(path, {"symbol": symbol, "from": (end - timedelta(days=days)).strftime("%d-%m-%Y"),
                                 "to": end.strftime("%d-%m-%Y")}))


def bulk_block_deals(symbol: str) -> dict:
    out: dict[str, Any] = {}
    for kind in ("bulk", "block"):
        try:
            rows = _deals(kind, symbol)
            out[kind] = [{"date": first(r, "BD_DT_DATE", "date"), "client": first(r, "BD_CLIENT_NAME", "clientName"),
                          "side": first(r, "BD_BUY_SELL", "buySell"), "qty": first(r, "BD_QTY_TRD", "quantity"),
                          "price": first(r, "BD_TP_WATP", "price")} for r in rows[:15]]
        except ProviderError as e:
            out[kind] = f"unavailable: {e}"
    return out


def shareholding(symbol: str) -> list[dict]:
    rows = _rows(get_json("/api/corporate-share-holdings-master", {"index": "equities", "symbol": symbol}))
    keep = ("date", "pr_and_prgrp", "public_val", "employeeTrusts", "Promoter", "FII", "DII", "Public")
    return [{k: v for k, v in r.items() if any(x.lower() in k.lower() for x in keep)} for r in rows[:4]]
