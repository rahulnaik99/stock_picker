"""Rates & macro: RBI policy/press releases, foreign central-bank rates (FRED), market snapshot, policy calendar."""
from __future__ import annotations

import csv
import io
import json
from datetime import timedelta
from typing import Any

import feedparser
import requests

from app.mcp_server.tools.common import (UA, error, http_get, now_ist, ok, parse_any_dt, parse_as_of, unavailable, SourceError)
from app.service.settings import ROOT, get_settings

CONFIG = ROOT / "app" / "config"
RBI_FEEDS = ["https://rbi.org.in/pressreleases_rss.xml", "https://rbi.org.in/notifications_rss.xml"]
_RBI_KEYWORDS = ("monetary policy", "repo", "mpc", "crr", "slr", "liquidity", "penalty", "directions", "banking regulation",
                 "nbfc", "housing finance", "credit", "lending", "interest rate", "basel", "capital adequacy", "microfinance")


def rbi_policy(days: int = 45, as_of: str | None = None) -> dict[str, Any]:
    """RBI policy repo rate (from config/policy_rates.json: keep it updated from the latest MPC release),
    next MPC dates, and recent RBI press releases/notifications relevant to banks and NBFCs."""
    src = "rbi"
    try:
        t = parse_as_of(as_of)
        policy = json.loads((CONFIG / "policy_rates.json").read_text())
        age = None
        if policy.get("as_of"):
            d = parse_any_dt(policy["as_of"])
            age = (t - d).days if d else None
        mpc = [r for r in _read_calendar() if r["category"] == "rbi_policy" and r["end_date"] >= t.date().isoformat()][:2]
        items: list[dict[str, Any]] = []
        notes: list[str] = []
        sess = requests.Session()
        sess.headers["User-Agent"] = UA
        for url in RBI_FEEDS:
            try:
                resp = http_get(sess, url)
                if not resp.ok:
                    notes.append(f"{url}: HTTP {resp.status_code}")
                    continue
                for e in feedparser.parse(resp.content).entries:
                    title = e.get("title", "")
                    pub = parse_any_dt(e.get("published") or e.get("updated")) or None
                    try:
                        from email.utils import parsedate_to_datetime
                        pub = parsedate_to_datetime(e.get("published", "")) if e.get("published") else pub
                    except (TypeError, ValueError):
                        pass
                    if pub is not None and pub.tzinfo and pub > t:
                        continue
                    if pub is not None and pub < t - timedelta(days=days):
                        continue
                    if any(k in title.lower() for k in _RBI_KEYWORDS):
                        items.append({"title": title, "published_at": pub.isoformat() if pub else None, "url": e.get("link")})
            except (SourceError, Exception) as exc:  # noqa: BLE001
                notes.append(f"{url}: {exc}")
        data = {"policy_rates": {**policy, "record_age_days": age,
                                 "needs_verification": policy.get("rbi_repo_rate_percent") is None or (age or 0) > 120},
                "next_mpc_meetings": mpc, "recent_rbi_releases": items[:15], "feed_notes": notes}
        return ok(src, data)
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)


def _fred_series(series_id: str, session: requests.Session) -> list[tuple[str, float]]:
    key = get_settings().fred_api_key
    if key:
        resp = http_get(session, "https://api.stlouisfed.org/fred/series/observations",
                        params={"series_id": series_id, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": "120"})
        resp.raise_for_status()
        obs = [(o["date"], float(o["value"])) for o in resp.json().get("observations", []) if o["value"] not in (".", "")]
        return sorted(obs)
    resp = http_get(session, "https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": series_id})
    resp.raise_for_status()
    rows = list(csv.reader(io.StringIO(resp.text)))[1:]
    return [(r[0], float(r[1])) for r in rows if len(r) > 1 and r[1] not in (".", "")][-120:]


def foreign_rates(as_of: str | None = None) -> dict[str, Any]:
    """Foreign policy/market rates from FRED (US Fed funds, US 10Y, ECB, UK, Japan): latest value, 1m/3m change,
    last observation date and a `stale` flag. Foreign central-bank moves drive FII flows and USD/INR."""
    src = "fred"
    try:
        t = parse_as_of(as_of)
        cfg = json.loads((CONFIG / "foreign_rates.json").read_text())
        sess = requests.Session()
        sess.headers["User-Agent"] = UA
        out, problems = [], []
        for s in cfg["series"]:
            try:
                obs = [(d, v) for d, v in _fred_series(s["id"], sess) if d <= t.date().isoformat()]
                if not obs:
                    problems.append(f"{s['id']}: no observations")
                    continue
                last_d, last_v = obs[-1]

                def back(days: int):
                    cut = (parse_any_dt(last_d) - timedelta(days=days)).date().isoformat()
                    prior = [v for d, v in obs if d <= cut]
                    return round(last_v - prior[-1], 3) if prior else None

                age = (t - parse_any_dt(last_d)).days
                out.append({"series": s["id"], "name": s["name"], "latest": last_v, "date": last_d, "change_1m": back(30),
                            "change_3m": back(90), "age_days": age, "stale": age > s.get("max_age_days", 30)})
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{s['id']}: {exc}")
        if not out:
            return error(src, "; ".join(problems) or "no series")
        return ok(src, out, problems=problems)
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)


def macro_snapshot(as_of: str | None = None) -> dict[str, Any]:
    """Market context via Yahoo: USD/INR, India VIX, Nifty Bank, Nifty 50, Brent, DXY (last close, 5d and 20d % change)."""
    src = "yfinance"
    try:
        import yfinance as yf

        t = parse_as_of(as_of)
        cfg = json.loads((CONFIG / "foreign_rates.json").read_text())["market_tickers"]
        out = []
        for name, ticker in cfg.items():
            try:
                h = yf.download(ticker, start=(t - timedelta(days=60)).date().isoformat(), end=(t.date() + timedelta(days=1)).isoformat(),
                                interval="1d", progress=False, auto_adjust=False, multi_level_index=False)
                c = h["Close"].dropna()
                if len(c) < 21:
                    continue
                out.append({"name": name, "last": round(float(c.iloc[-1]), 3), "date": str(c.index[-1].date()),
                            "chg_5d_pct": round((c.iloc[-1] / c.iloc[-6] - 1) * 100, 2), "chg_20d_pct": round((c.iloc[-1] / c.iloc[-21] - 1) * 100, 2)})
            except Exception:  # noqa: BLE001 - skip one ticker
                continue
        return ok(src, out) if out else unavailable(src, "no market data returned")
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)


def _read_calendar() -> list[dict[str, str]]:
    with open(CONFIG / "macro_events.csv", newline="") as fh:
        return list(csv.DictReader(fh))


def policy_calendar(days_ahead: int = 60, as_of: str | None = None) -> dict[str, Any]:
    """Upcoming RBI MPC and US FOMC meetings from the versioned calendar CSV (verify dates against the official sites)."""
    try:
        t = parse_as_of(as_of)
        horizon = (t + timedelta(days=days_ahead)).date().isoformat()
        rows = [r for r in _read_calendar() if t.date().isoformat() <= r["end_date"] <= horizon]
        return ok("config/macro_events.csv", rows, note="Static calendar; confirm on rbi.org.in and federalreserve.gov.")
    except Exception as exc:  # noqa: BLE001
        return error("macro_events.csv", exc)
