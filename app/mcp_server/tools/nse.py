"""NSE tools: announcements, board meetings, corporate actions, FII/DII, bulk/block deals, shareholding.

NSE's JSON endpoints are unofficial and bot-protected. We warm a session (cookies) first; if NSE still
blocks us the tool returns status=error with a hint to set NSE_COOKIES, and the agent marks data missing.
Every event gets published_at / event_date / retrieved_at / available_at as required by the architecture.
"""
from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timedelta
from typing import Any

import requests

from app.mcp_server.tools.common import (SourceError, UA, clean_symbol, error, http_get, now_ist, ok, parse_any_dt,
                                         parse_as_of, unavailable, window)
from app.service.logger import get_logger
from app.service.settings import get_settings
from app.store import events as event_store
from app.store import flows as flow_store
from app.store.db import mark_refreshed, refreshed_today

log = get_logger(__name__)
BASE = "https://www.nseindia.com"
_lock = threading.Lock()
_session: requests.Session | None = None
_warmed_at: datetime | None = None

_EVENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("results", ("financial result", "quarterly result", "audited", "unaudited", "outcome of board meeting")),
    ("dividend", ("dividend",)),
    ("buyback", ("buyback", "buy back", "buy-back")),
    ("split_bonus", ("split", "sub-division", "bonus")),
    ("rights_issue", ("rights issue", "rights entitlement")),
    ("fundraise", ("qip", "preferential", "non-convertible", "ncd", "fund raising", "fund-raising", "bonds", "capital raising", "ipo", "ofs")),
    ("merger_acquisition", ("merger", "amalgamation", "acquisition", "demerger", "scheme of arrangement", "stake")),
    ("credit_rating", ("credit rating", "rating action", "crisil", "icra", "care ratings", "india ratings")),
    ("regulatory_action", ("penalty", "rbi", "sebi", "irdai", "show cause", "directions", "fine")),
    ("management_change", ("resignation", "appointment", "cessation", "managing director", "ceo", "cfo", "key managerial")),
    ("analyst_meet", ("analyst", "investor meet", "earnings call", "conference call", "transcript")),
    ("board_meeting", ("board meeting",)),
]


def classify_event(text: str) -> str:
    t = text.lower()
    for kind, words in _EVENT_RULES:
        if any(w in t for w in words):
            return kind
    return "other"


def _event_id(symbol: str, event_type: str, source: str, doc_key: str) -> str:
    return hashlib.sha1(f"{symbol}|{event_type}|{source}|{doc_key}".encode()).hexdigest()[:16]


def _get_session() -> requests.Session:
    global _session, _warmed_at
    s = get_settings()
    with _lock:
        if _session is None or (_warmed_at and now_ist() - _warmed_at > timedelta(minutes=20)):
            _session = requests.Session()
            _session.headers.update({"User-Agent": s.nse_user_agent or UA, "Accept": "application/json, text/plain, */*",
                                     "Accept-Language": "en-US,en;q=0.9", "Referer": f"{BASE}/"})
            if s.nse_cookies:
                _session.headers["Cookie"] = s.nse_cookies
            else:
                try:  # warm-up request sets the cookies NSE expects
                    _session.get(BASE, headers={"Accept": "text/html"}, timeout=15)
                except requests.RequestException:
                    pass
            _warmed_at = now_ist()
    return _session


def nse_get(path: str, params: dict[str, str] | None = None, referer: str | None = None) -> Any:
    headers = {"Referer": referer} if referer else None
    resp = http_get(_get_session(), f"{BASE}{path}", params=params, headers=headers)
    if resp.status_code in (401, 403):
        raise SourceError("NSE blocked the request (HTTP %s). Set NSE_COOKIES in .env from your browser." % resp.status_code)
    if not resp.ok:
        raise SourceError(f"NSE HTTP {resp.status_code} for {path}")
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError("NSE returned a non-JSON page (bot check). Set NSE_COOKIES in .env.") from exc


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("rows") or []
    return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []


def nse_get_url(url: str, referer: str | None = None) -> Any:
    """GET an absolute NSE URL (e.g. a resultDetailedDataLink) and return parsed JSON."""
    headers = {"Referer": referer} if referer else None
    resp = http_get(_get_session(), url, headers=headers)
    if resp.status_code in (401, 403):
        raise SourceError("NSE blocked the request (HTTP %s). Set NSE_COOKIES in .env from your browser." % resp.status_code)
    if not resp.ok:
        raise SourceError(f"NSE HTTP {resp.status_code} for {url}")
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError("NSE returned a non-JSON page (bot check). Set NSE_COOKIES in .env.") from exc


def nse_get_bytes(url: str, referer: str | None = None) -> tuple[bytes, int]:
    """Fetch a raw document (e.g. an XBRL file on nsearchives) through the warmed NSE session. Returns (content, http_status)."""
    headers = {"Referer": referer, "Accept": "application/xml, text/xml, */*"} if referer else {"Accept": "application/xml, text/xml, */*"}
    resp = http_get(_get_session(), url, headers=headers)
    if resp.status_code in (401, 403):
        raise SourceError("NSE blocked the request (HTTP %s). Set NSE_COOKIES in .env from your browser." % resp.status_code)
    if not resp.ok:
        raise SourceError(f"NSE HTTP {resp.status_code} for {url}")
    return resp.content, resp.status_code


def _fmt(d: datetime) -> str:
    return d.strftime("%d-%m-%Y")


def _norm_event(*, symbol: str, event_type: str, source: str, doc_key: str, summary: str, url: str,
                published_at: datetime | None, event_date: datetime | None, status: str, retrieved: datetime) -> dict[str, Any]:
    return {
        "event_id": _event_id(symbol, event_type, source, doc_key), "symbol": symbol, "exchange": "NSE", "event_type": event_type,
        "announcement_date": published_at.date().isoformat() if published_at else "",
        "event_date": event_date.date().isoformat() if event_date else "", "event_status": status,
        "event_summary": re.sub(r"\s+", " ", summary).strip()[:400], "source_url": url,
        "published_at": published_at.isoformat() if published_at else "", "retrieved_at": retrieved.isoformat(timespec="seconds"),
        # available_at = when we can prove the market could know it: publication time, else the time we first saw it
        "available_at": (published_at or retrieved).isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------ per-stock event feeds
def nse_company_events(symbol: str, days: int = 90, as_of: str | None = None) -> dict[str, Any]:
    """Official NSE announcements + board meetings + corporate actions (dividend, split, bonus, buyback ...) for one stock,
    normalised to the event-ledger schema. Only items available by `as_of` are returned (point-in-time)."""
    src = "nse"
    sym = clean_symbol(symbol)
    t = parse_as_of(as_of)
    start, end = window(t, days)
    retrieved = now_ist()
    events: list[dict[str, Any]] = []
    errors: list[str] = []

    def feed(name: str, fn) -> None:
        try:
            events.extend(fn())
        except Exception as exc:  # noqa: BLE001 - one broken feed must not hide the others
            errors.append(f"{name}: {exc}")

    def announcements():
        rows = _rows(nse_get("/api/corporate-announcements", {"index": "equities", "symbol": sym, "from_date": _fmt(start), "to_date": _fmt(end)}))
        for r in rows:
            pub = parse_any_dt(r.get("an_dt") or r.get("sort_date"))
            text = f"{r.get('desc', '')}. {r.get('attchmntText', '')}"
            if pub and pub > t:
                continue
            kind = classify_event(text)
            url = r.get("attchmntFile") or f"{BASE}/companies-listing/corporate-filings-announcements"
            yield _norm_event(symbol=sym, event_type=kind, source="announcement", doc_key=str(r.get("seq_id") or url or pub),
                              summary=text, url=url, published_at=pub, event_date=pub, status="announced", retrieved=retrieved)

    def board_meetings():
        for r in _rows(nse_get("/api/corporate-board-meetings", {"index": "equities", "symbol": sym})):
            day = parse_any_dt(r.get("bm_date"))
            if day is None or not (start - timedelta(days=1) <= day <= end + timedelta(days=60)):
                continue
            purpose = f"{r.get('bm_purpose', '')}. {r.get('bm_desc', '')}"
            kind = classify_event(purpose)
            yield _norm_event(symbol=sym, event_type=kind if kind != "other" else "board_meeting", source="board_meeting",
                              doc_key=str(day.date()), summary=f"Board meeting {day.date()}: {purpose}",
                              url=f"{BASE}/companies-listing/corporate-filings-board-meetings", published_at=None, event_date=day,
                              status="scheduled" if day > t else "held", retrieved=retrieved)

    def actions():
        for r in _rows(nse_get("/api/corporates-corporateActions", {"index": "equities", "symbol": sym, "from_date": _fmt(start), "to_date": _fmt(end + timedelta(days=90))})):
            subject = str(r.get("subject") or r.get("purpose") or "Corporate action")
            ex = parse_any_dt(r.get("exDate")); rec = parse_any_dt(r.get("recDate"))
            day = ex or rec
            if day is None:
                continue
            kind = classify_event(subject)
            yield _norm_event(symbol=sym, event_type=kind if kind != "other" else "corporate_action", source="corporate_action",
                              doc_key=f"{subject}|{day.date()}", summary=f"{subject} (ex-date {ex.date() if ex else 'n/a'}, record {rec.date() if rec else 'n/a'})",
                              url=f"{BASE}/companies-listing/corporate-filings-actions", published_at=None, event_date=day,
                              status="upcoming" if day > t else "done", retrieved=retrieved)

    live = t.date() >= now_ist().date()
    # Include `days` in the gate key so a 90-day fetch doesn't block a later 120-day fetch from the same run.
    # Both agents calling with the same window reuse the cache; different windows re-fetch once.
    gate_key = f"{sym}:{days}" if live else f"{sym}@{t.date()}:{days}"
    cache, new = "hit", 0
    if not refreshed_today("events", gate_key):
        cache = "refreshed"
        feed("announcements", announcements)
        feed("board_meetings", board_meetings)
        feed("corporate_actions", actions)
        if events:
            new = event_store.upsert(events)
            mark_refreshed("events", gate_key)
    stored = event_store.load(sym, t, days)
    if not stored and errors:
        return error(src, " | ".join(errors[:3]))
    stored.sort(key=lambda e: e["available_at"], reverse=True)
    return ok(src, stored[:80], symbol=sym, feed_errors=errors, window=f"{start.date()}..{end.date()}", cache=cache, new_events=new)


# ------------------------------------------------------------------ flows
def nse_fii_dii(as_of: str | None = None) -> dict[str, Any]:
    """MARKET-WIDE FII/DII cash-market flows. NSE shows the latest session only, so each day is stored in SQLite (upsert by day)
    and 1/5/20-day net flows, streaks and momentum are computed in Python from the stored history."""
    src = "nse_fii_dii (SQLite history)"
    try:
        t = parse_as_of(as_of)
        live = t.date() >= now_ist().date()
        cache, fetch_error, latest_rows = "hit", None, []
        if live and not refreshed_today("fii_dii", "market"):
            cache = "refreshed"
            try:
                rows = _rows(nse_get("/api/fiidiiTradeReact"))
                latest_rows = [{"category": r.get("category"), "date": r.get("date"), "buy_value_cr": r.get("buyValue"),
                                "sell_value_cr": r.get("sellValue"), "net_value_cr": r.get("netValue")} for r in rows]
                if flow_store.upsert_rows(latest_rows):
                    mark_refreshed("fii_dii", "market")
            except Exception as exc:  # noqa: BLE001 - keep serving stored history
                fetch_error, cache = f"{type(exc).__name__}: {str(exc)[:160]}", "stale"
        stats = flow_store.analytics(t.date())
        if not any(v.get("days_available") for v in stats.values()):
            return (error(src, fetch_error) if fetch_error else unavailable(src, "no FII/DII rows stored or returned by NSE"))
        return ok(src, {"latest_rows": latest_rows, "analytics": stats}, scope="market_wide_not_stock_specific", cache=cache,
                  fetch_error=fetch_error,
                  note="Net values in INR crore. Windows show 'insufficient_history' until enough daily rows are stored "
                       "(run daily, or backfill with `python -m app.jobs.import_fii_dii file.csv`).")
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)


def nse_deals(symbol: str, days: int = 30, as_of: str | None = None) -> dict[str, Any]:
    """STOCK-SPECIFIC bulk and block deals (large buyer/seller names, quantities).
    Cached daily so the two agents that call this (TrendFlow + Trader) share one NSE hit per stock."""
    import time
    from app.store.db import kv_get, kv_set
    src = "nse_deals"
    sym = clean_symbol(symbol)
    t = parse_as_of(as_of)
    start, end = window(t, days)
    gate_key = f"{sym}:{days}"
    # Return cached result for today if already fetched (avoids double NSE hit from concurrent agents)
    if refreshed_today("deals", gate_key):
        cached = kv_get("deals", gate_key)
        if cached is not None:
            return ok(src, cached, symbol=sym, scope="stock_specific", cache="hit",
                      note="Empty list = no reported deals in window.")
    out: list[dict[str, Any]] = []
    errs: list[str] = []
    for kind in ("bulk-deals", "block-deals"):
        for attempt in range(2):                     # one immediate retry on 503 (endpoint is intermittently flaky)
            try:
                for r in _rows(nse_get(f"/api/historical/{kind}", {"symbol": sym, "from": _fmt(start), "to": _fmt(end)})):
                    day = parse_any_dt(r.get("BD_DT_DATE") or r.get("date"))
                    if day and day > t:
                        continue
                    out.append({"type": kind[:-1], "date": str(day.date()) if day else None,
                                "client": r.get("BD_CLIENT_NAME") or r.get("clientName"),
                                "side": r.get("BD_BUY_SELL") or r.get("buySell"),
                                "quantity": r.get("BD_QTY_TRD") or r.get("quantity"),
                                "avg_price": r.get("BD_TP_WATP") or r.get("watp")})
                break
            except SourceError as exc:
                if "503" in str(exc) and attempt == 0:
                    time.sleep(0.4)
                    continue                          # retry once
                errs.append(f"{kind}: {exc}")
                break
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{kind}: {exc}")
                break
    # 503 = NSE endpoint temporarily unavailable; return ok with empty list so the agent sees
    # "no deals retrieved" rather than a hard error that pollutes the missing_data list.
    if not out and errs and all("503" in e for e in errs):
        return ok(src, [], symbol=sym, scope="stock_specific", feed_errors=errs,
                  note="NSE bulk/block-deals endpoint temporarily unavailable (HTTP 503). Empty list returned.")
    if not out and errs:
        return error(src, " | ".join(errs))
    # Cache the result so the second caller (Trader or TrendFlow) skips the NSE round-trip today.
    kv_set("deals", gate_key, out)
    mark_refreshed("deals", gate_key)
    return ok(src, out, symbol=sym, scope="stock_specific", feed_errors=errs,
              note="Empty list = no reported deals in window.")


def nse_shareholding(symbol: str) -> dict[str, Any]:
    """STOCK-SPECIFIC quarterly shareholding pattern (promoter / public etc.) from NSE filings."""
    src = "nse_shareholding"
    sym = clean_symbol(symbol)
    try:
        rows = _rows(nse_get("/api/corporate-share-holdings-master", {"index": "equities", "symbol": sym}))
        if not rows:
            return unavailable(src, f"no shareholding rows for {sym}")
        keep = ("date", "pr_and_prgrp", "public_val", "employeeTrusts", "recordDate", "name")
        return ok(src, [{k: r.get(k) for k in keep if k in r} for r in rows[:6]], symbol=sym, scope="stock_specific")
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)


def nse_promoter_pledge(symbol: str) -> dict[str, Any]:
    """STOCK-SPECIFIC promoter pledging data from NSE filings.
    Returns quarterly pledge %, trend (rising/falling/stable) and the latest raw rows.
    Pledge > 30% = caution; pledge > 50% = high risk; falling pledge = positive signal."""
    src = "nse_promoter_pledge"
    sym = clean_symbol(symbol)
    ref = f"{BASE}/get-quote/equity/{sym}"
    try:
        rows = _rows(nse_get("/api/corporatePledgeData",
                             {"index": "equities", "symbol": sym, "type": "quarterly"}, referer=ref))
        if not rows:
            return unavailable(src, f"no pledging data for {sym} (may not be an F&O stock or pledging not disclosed)")
        keep = ("pledgeDate", "quarter", "promotersPledgedShares", "percentOfTotalShares",
                "percentOfPromotersHoldings", "totPromoterShares")
        cleaned = [{k: r.get(k) for k in keep if k in r} for r in rows[:8]]
        # Trend: compare latest quarter pledge % to oldest available
        pcts = []
        for r in cleaned:
            v = r.get("percentOfPromotersHoldings") or r.get("percentOfTotalShares")
            try:
                pcts.append(float(str(v).replace("%", "").strip()))
            except (TypeError, ValueError):
                pass
        latest_pledge = pcts[0] if pcts else None
        trend = ("rising" if len(pcts) >= 2 and pcts[0] > pcts[-1] + 2 else
                 "falling" if len(pcts) >= 2 and pcts[0] < pcts[-1] - 2 else "stable")
        risk = ("high" if latest_pledge and latest_pledge > 50 else
                "caution" if latest_pledge and latest_pledge > 25 else "low")
        return ok(src, {"latest_pledge_pct": latest_pledge, "trend": trend, "risk": risk,
                        "quarters": cleaned,
                        "note": "pledge_pct is % of PROMOTER holdings pledged. >30%=caution; >50%=high risk."},
                  symbol=sym, scope="stock_specific")
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)
