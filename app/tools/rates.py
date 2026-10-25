"""Rates and policy: RBI repo rate + RBI updates (official), Fed / US10Y / USDINR (FRED), ECB deposit rate (ECB SDW).
BoE / BoJ are intentionally not automated (no stable free API); add them in app/config/sectors.json queries if wanted."""
from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from app.service.settings import get_settings
from app.tools.common import IST, envelope, http_get, log, parse_as_of, unavailable

RBI_FEEDS = {"press_release": "https://rbi.org.in/pressreleases_rss.xml", "notification": "https://rbi.org.in/notifications_rss.xml"}
RBI_KEYWORDS = re.compile(r"repo|monetary policy|mpc|crr|slr|liquidity|nbfc|bank|credit|lending|penalty|basel|priority sector|msme|housing|forex|rupee|inflation", re.I)
FRED = "https://api.stlouisfed.org/fred/series/observations"
ECB = "https://data-api.ecb.europa.eu/service/data/FM/B.U2.EUR.4F.KR.DFR.LEV"


def _rss(url: str) -> list[dict]:
    root = ET.fromstring(http_get(url, as_json=False))
    out = []
    for it in root.iter("item"):
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "")
            dt = dt if dt.tzinfo else dt.replace(tzinfo=IST)
        except (TypeError, ValueError):
            dt = None
        out.append({"title": (it.findtext("title") or "").strip(), "url": (it.findtext("link") or "").strip(), "published_at": dt})
    return out


def get_rbi_updates(days: int = 30, as_of: str = "") -> dict:
    """Recent RBI press releases / notifications relevant to banks, NBFCs, liquidity and policy."""
    cutoff = parse_as_of(as_of)
    items, errors = [], []
    for kind, url in RBI_FEEDS.items():
        try:
            for it in _rss(url):
                if it["published_at"] and cutoff - timedelta(days=days) <= it["published_at"] <= cutoff and RBI_KEYWORDS.search(it["title"]):
                    items.append({**it, "kind": kind, "published_at": it["published_at"].isoformat()})
        except Exception as exc:
            errors.append(f"{kind}: {exc}")
    if not items and errors:
        return unavailable("rbi.org.in RSS", "; ".join(errors), as_of)
    items.sort(key=lambda x: x["published_at"], reverse=True)
    return envelope("partial" if errors else "ok", "rbi.org.in RSS", items[:25], "; ".join(errors) or None, as_of)


_REPO = re.compile(r"policy\s+repo\s+rate[^.]{0,250}?(?:at|to)\s+(\d+(?:\.\d+)?)\s*(?:per\s*cent|%)", re.I | re.S)


def _rbi_repo(cutoff: datetime) -> dict:
    mpc = [i for i in _rss(RBI_FEEDS["press_release"]) if re.search(r"monetary policy statement", i["title"], re.I)
           and i["published_at"] and i["published_at"] <= cutoff]
    if not mpc:
        raise RuntimeError("no 'Monetary Policy Statement' in the RBI press-release feed window")
    latest = max(mpc, key=lambda i: i["published_at"])
    text = re.sub(r"<[^>]+>", " ", http_get(latest["url"], as_json=False))
    m = _REPO.search(re.sub(r"\s+", " ", text))
    if not m:
        raise RuntimeError(f"could not parse repo rate from {latest['url']}")
    return {"value_pct": float(m.group(1)), "decision_date": latest["published_at"].date().isoformat(), "source_url": latest["url"],
            "note": "parsed from the RBI statement text; verify against the document"}


def _fred(series: str, cutoff: datetime) -> dict:
    key = get_settings().fred_api_key
    if not key:
        raise RuntimeError("FRED_API_KEY not set")
    body = http_get(FRED, params={"series_id": series, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 10,
                                  "observation_end": cutoff.date().isoformat()})
    obs = [o for o in body.get("observations", []) if o.get("value") not in (".", None)]
    if not obs:
        raise RuntimeError(f"no observations for {series}")
    return {"value": float(obs[0]["value"]), "observation_date": obs[0]["date"], "series": series}


def _ecb(cutoff: datetime) -> dict:
    text = http_get(ECB, params={"lastNObservations": 5, "format": "csvdata", "endPeriod": cutoff.date().isoformat()}, as_json=False)
    rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("OBS_VALUE")]
    if not rows:
        raise RuntimeError("no ECB observations")
    last = rows[-1]
    return {"value_pct": float(last["OBS_VALUE"]), "observation_date": last["TIME_PERIOD"], "series": "ECB deposit facility rate"}


def get_policy_rates(as_of: str = "") -> dict:
    """RBI repo rate, Fed funds target range, ECB deposit rate, US 10Y yield, USD/INR. Each item has its own status."""
    cutoff = parse_as_of(as_of)
    jobs = {"rbi_repo_rate": lambda: _rbi_repo(cutoff), "fed_target_upper_pct": lambda: _fred("DFEDTARU", cutoff),
            "fed_target_lower_pct": lambda: _fred("DFEDTARL", cutoff), "us_10y_yield_pct": lambda: _fred("DGS10", cutoff),
            "usd_inr": lambda: _fred("DEXINUS", cutoff), "ecb_deposit_rate": lambda: _ecb(cutoff)}
    data, ok = {}, 0
    for name, fn in jobs.items():
        try:
            data[name] = {"status": "ok", **fn()}
            ok += 1
        except Exception as exc:
            log.warning("policy rate %s failed: %s", name, exc)
            data[name] = {"status": "unavailable", "error": str(exc)}
    data["not_covered"] = ["BoE bank rate", "BoJ policy rate"]
    status = "ok" if ok == len(jobs) else ("partial" if ok else "unavailable")
    return envelope(status, "RBI / FRED / ECB", data, None if ok else "all rate sources failed", as_of,
                    caveat="rate level alone is not the signal; direction of change and surprise vs expectations matter")
