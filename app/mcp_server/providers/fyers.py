"""FYERS API v3 - READ-ONLY market data (candles, option chain). No order endpoints exist here.

Auth header is "app_id:access_token". The token expires daily: run `python scripts/fyers_login.py`.
Endpoints follow FYERS v3 docs; verify against the current docs if FYERS changes them.
"""
from __future__ import annotations

import csv
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from filelock import FileLock

from app.mcp_server.providers.common import IST, ProviderError, Unavailable, first
from app.mcp_server.providers.symbols import fyers_symbol
from app.service.logger import get_logger, short
from app.service.settings import get_settings

log = get_logger(__name__)
HISTORY_URL = "https://api-t1.fyers.in/data/history"
OPTION_CHAIN_URL = "https://api-t1.fyers.in/data/options-chain-v3"


class _Limiter:
    """Sliding window: requests/second and requests/minute (thread-safe)."""

    def __init__(self, rps: int, rpm: int) -> None:
        self.rps, self.rpm = rps, rpm
        self.sec: deque[float] = deque()
        self.min: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.sec and now - self.sec[0] >= 1:
                    self.sec.popleft()
                while self.min and now - self.min[0] >= 60:
                    self.min.popleft()
                wait = 0.0
                if len(self.sec) >= self.rps:
                    wait = max(wait, 1 - (now - self.sec[0]))
                if len(self.min) >= self.rpm:
                    wait = max(wait, 60 - (now - self.min[0]))
                if wait <= 0:
                    self.sec.append(now)
                    self.min.append(now)
                    return
            time.sleep(wait + 0.01)


_limiter: _Limiter | None = None


def _auth() -> dict[str, str]:
    s = get_settings()
    if not (s.fyers_app_id and s.fyers_access_token):
        raise Unavailable("FYERS not configured (set FYERS_APP_ID and FYERS_ACCESS_TOKEN in .env)")
    return {"Authorization": f"{s.fyers_app_id}:{s.fyers_access_token}"}


def _get(url: str, params: dict) -> dict:
    global _limiter
    s = get_settings()
    headers = _auth()
    if _limiter is None:
        _limiter = _Limiter(s.fyers_rps, s.fyers_rpm)
    last: Exception | None = None
    for attempt in range(max(1, s.http_retries)):
        _limiter.acquire()
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=s.http_timeout)
        except httpx.HTTPError as e:
            last = e
            time.sleep(0.8 * 2 ** attempt)
            continue
        if r.status_code in (401, 403):
            raise ProviderError("FYERS authentication failed - access token expired? Run scripts/fyers_login.py")
        if r.status_code == 429 or r.status_code >= 500:
            last = ProviderError(f"FYERS HTTP {r.status_code}")
            time.sleep(1.0 * 2 ** attempt)
            continue
        try:
            body = r.json()
        except ValueError as e:
            raise ProviderError(f"FYERS returned non-JSON (HTTP {r.status_code})") from e
        if body.get("s") != "ok":
            msg = str(body.get("message", body))
            if any(w in msg.lower() for w in ("token", "authenticat", "unauthor")):
                raise ProviderError(f"FYERS authentication failed: {msg}")
            raise ProviderError(f"FYERS error: {short(msg, 200)}")
        return body
    raise ProviderError(f"FYERS request failed after retries: {last}")


# ----------------------------------------------------------------------------- candles
def history(symbol: str, resolution: str, days: int, as_of: datetime) -> pd.DataFrame:
    """Candles ending at as_of. resolution: 'D' or minutes ('15'). Chunked to FYERS range limits."""
    max_span = 360 if resolution == "D" else 95
    end = as_of.astimezone(IST).date()
    start = end - timedelta(days=days)
    rows: list[list[float]] = []
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=max_span))
        body = _get(HISTORY_URL, {"symbol": fyers_symbol(symbol), "resolution": resolution, "date_format": "1",
                                  "range_from": cur.isoformat(), "range_to": chunk_end.isoformat(), "cont_flag": "1"})
        rows += body.get("candles") or []
        cur = chunk_end + timedelta(days=1)
    if not rows:
        raise Unavailable(f"FYERS returned no {resolution} candles for {symbol}")
    df = pd.DataFrame(rows, columns=["epoch", "open", "high", "low", "close", "volume"]).drop_duplicates("epoch")
    df["ts"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert(IST)
    if resolution == "D":  # daily bars arrive stamped 05:30 IST; normalise to the session date
        df["ts"] = df["ts"].dt.normalize()
    df = df.sort_values("ts")[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    bad = df[(df["high"] < df["low"]) | (df["close"] <= 0)]
    if len(bad):
        raise ProviderError(f"FYERS returned {len(bad)} invalid candles for {symbol}")
    return df


def candles_bundle(symbol: str, as_of: datetime) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    daily = history(symbol, "D", 430, as_of)
    try:
        intraday = history(symbol, "15", 10, as_of)
    except (Unavailable, ProviderError) as e:
        log.warning("%s: FYERS 15m candles unavailable (%s); VWAP skipped", symbol, e)
        intraday = None
    return daily, intraday


# ----------------------------------------------------------------------------- option chain
def option_chain(symbol: str, strikecount: int = 15) -> dict:
    try:
        body = _get(OPTION_CHAIN_URL, {"symbol": fyers_symbol(symbol), "strikecount": strikecount})
    except ProviderError as e:
        if "authentication" in str(e).lower():
            raise
        raise Unavailable(f"no option chain for {symbol} (not in F&O?): {e}") from e
    data = body.get("data") or {}
    if not data.get("optionsChain"):
        raise Unavailable(f"empty option chain for {symbol} (not in F&O?)")
    return data


def summarize_option_chain(data: dict, symbol: str) -> dict:
    chain = data["optionsChain"]
    calls = [x for x in chain if x.get("option_type") == "CE"]
    puts = [x for x in chain if x.get("option_type") == "PE"]
    under = next((x for x in chain if not x.get("option_type") or x.get("strike_price") in (-1, None)), {})
    spot = under.get("ltp")

    def tot(rows: list[dict], key: str) -> float:
        return float(sum((x.get(key) or 0) for x in rows))

    c_oi, p_oi = tot(calls, "oi"), tot(puts, "oi")
    c_ch, p_ch = tot(calls, "oich"), tot(puts, "oich")

    def top(rows: list[dict]) -> list[dict]:
        rows = sorted(rows, key=lambda x: x.get("oi") or 0, reverse=True)[:3]
        return [{"strike": x.get("strike_price"), "oi": x.get("oi"), "oi_change": x.get("oich")} for x in rows]

    atm = min((x["strike_price"] for x in calls if x.get("strike_price", -1) > 0),
              key=lambda k: abs(k - spot), default=None) if spot else None
    expiries = [e.get("date") for e in (data.get("expiryData") or [])]
    summary = {
        "spot": spot, "atm_strike": atm, "nearest_expiry": expiries[0] if expiries else None,
        "expiries_available": expiries[:4],
        "total_call_oi": c_oi, "total_put_oi": p_oi,
        "pcr_oi": round(p_oi / c_oi, 3) if c_oi else None,
        "pcr_volume": round(tot(puts, "volume") / tot(calls, "volume"), 3) if tot(calls, "volume") else None,
        "call_oi_change_1d": c_ch, "put_oi_change_1d": p_ch,
        "top_call_oi_strikes": top(calls), "top_put_oi_strikes": top(puts),
        "india_vix": (data.get("indiavixData") or {}).get("ltp"),
        "strikes_covered": len(calls),
        "note": "oi_change_1d is vs previous session; weekly/monthly deltas come from stored snapshots (see windows).",
    }
    summary["windows"] = oi_windows(symbol, summary)
    return summary


def oi_windows(symbol: str, summary: dict) -> dict:
    """Append today's OI snapshot and compare with older snapshots for weekly/monthly deltas."""
    s = get_settings()
    path = s.path(s.data_dir) / "oi_snapshots.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(IST).date()
    with FileLock(str(path) + ".lock"):
        rows = list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []
        mine = [r for r in rows if r["symbol"] == symbol and r["expiry"] == str(summary["nearest_expiry"])]
        if not any(r["date"] == today.isoformat() for r in mine):
            new = {"date": today.isoformat(), "symbol": symbol, "expiry": summary["nearest_expiry"],
                   "call_oi": summary["total_call_oi"], "put_oi": summary["total_put_oi"]}
            write_header = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(new))
                if write_header:
                    w.writeheader()
                w.writerow(new)
    out: dict[str, Any] = {}
    for label, days in (("weekly", 7), ("monthly", 30)):
        cands = [r for r in mine if (today - date.fromisoformat(r["date"])).days >= days]
        if not cands:
            out[label] = "unavailable (no stored snapshot old enough yet)"
            continue
        ref = max(cands, key=lambda r: r["date"])
        out[label] = {"since": ref["date"], "call_oi_change": summary["total_call_oi"] - float(ref["call_oi"]),
                      "put_oi_change": summary["total_put_oi"] - float(ref["put_oi"])}
    return out
