"""Market data: candles (FYERS first, Yahoo fallback) and the FYERS option chain.

Provider is recorded on every result. Providers are never mixed inside one series.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests

from app.mcp_server.tools.common import (IST, SourceError, clean_symbol, error, fyers_symbol, http_get, now_ist,
                                         ok, parse_as_of, unavailable, yahoo_symbol)
from app.service.logger import get_logger
from app.service.settings import get_settings
from app.store.db import connect, kv_get, kv_set, mark_refreshed, refreshed_today

log = get_logger(__name__)
FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"
FYERS_OPTION_CHAIN_URL = "https://api-t1.fyers.in/data/options-chain-v3"
# FYERS: daily history <= 366 days per call, intraday <= 100 days per call.
_throttle_lock = threading.Lock()
_last_call = 0.0
_fyers_auth_warning_emitted = False


def _throttle(min_interval: float = 0.25) -> None:
    """FYERS allows ~5 requests/second; keep a gap between calls (the MCP server is one process)."""
    global _last_call
    with _throttle_lock:
        wait = _last_call + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


_RES = {"D": ("D", 366), "60": ("60", 100), "15": ("15", 100)}


# ------------------------------------------------------------------ candles
def _fyers_candles(symbol: str, resolution: str, start: datetime, end: datetime, session: requests.Session | None = None) -> pd.DataFrame:
    s = get_settings()
    if not (s.fyers_app_id and s.fyers_access_token):
        raise SourceError("FYERS_APP_ID / FYERS_ACCESS_TOKEN not set")
    sess = session or requests.Session()
    res, max_days = _RES[resolution]
    headers = {"Authorization": f"{s.fyers_app_id}:{s.fyers_access_token}"}
    rows: list[list[float]] = []
    cursor = start.date()
    while cursor <= end.date():
        chunk_end = min(end.date(), cursor + timedelta(days=max_days - 1))
        _throttle()
        resp = http_get(sess, FYERS_HISTORY_URL, headers=headers, params={
            "symbol": fyers_symbol(symbol), "resolution": res, "date_format": "1",
            "range_from": cursor.isoformat(), "range_to": chunk_end.isoformat(), "cont_flag": "1"})
        if resp.status_code in (401, 403):
            raise SourceError(f"FYERS authentication failed (HTTP {resp.status_code}: {resp.text[:200]}). "
                              "Token expired/invalid? Regenerate it with: python -m scripts.fyers_login")
        payload = resp.json()
        if payload.get("s") == "no_data":
            pass
        elif payload.get("s") != "ok":
            raise SourceError(f"FYERS error: {payload.get('message') or payload}")
        rows.extend(payload.get("candles") or [])
        cursor = chunk_end + timedelta(days=1)
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(IST)
    return df.drop(columns="ts").drop_duplicates("time").sort_values("time").reset_index(drop=True)


def _yahoo_candles(symbol: str, resolution: str, start: datetime, end: datetime) -> pd.DataFrame:
    import yfinance as yf

    interval = {"D": "1d", "60": "60m", "15": "15m"}[resolution]
    df = yf.download(yahoo_symbol(symbol), start=start.date().isoformat(), end=(end.date() + timedelta(days=1)).isoformat(),
                     interval=interval, auto_adjust=False, progress=False, multi_level_index=False)
    if df is None or df.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = df.reset_index()
    tcol = df.columns[0]
    t = pd.to_datetime(df[tcol])
    t = t.dt.tz_localize(IST) if t.dt.tz is None else t.dt.tz_convert(IST)
    return pd.DataFrame({"time": t, "open": df["Open"], "high": df["High"], "low": df["Low"],
                         "close": df["Close"], "volume": df["Volume"]}).reset_index(drop=True)


def fetch_candles(symbol: str, resolution: str, days: int, as_of: str | None = None) -> tuple[pd.DataFrame, str]:
    """Return (candles, provider). Drops the still-forming bar. FYERS first, Yahoo if FYERS is unusable."""
    end = parse_as_of(as_of)
    start = end - timedelta(days=days)
    provider = "fyers"
    try:
        df = _fyers_candles(symbol, resolution, start, end)
    except (SourceError, ValueError, requests.RequestException) as exc:
        global _fyers_auth_warning_emitted
        if "authentication failed" in str(exc).lower():
            if not _fyers_auth_warning_emitted:
                log.warning("FYERS auth failed, using Yahoo fallback (further auth warnings suppressed). Reason: %s", exc)
                log.warning("Regenerate the daily FYERS token with: python -m scripts.fyers_login")
                _fyers_auth_warning_emitted = True
            else:
                log.debug("FYERS authentication failed for %s; using Yahoo fallback", symbol)
        else:
            log.warning("FYERS candles failed for %s (%s); falling back to Yahoo", symbol, exc)
        provider = "yfinance"
        df = _yahoo_candles(symbol, resolution, start, end)
    if df.empty:
        return df, provider
    df = df[df["time"] <= end]
    # Closed candles only: drop today's daily bar before the close; drop an intraday bar that has not finished.
    now = now_ist()
    if resolution == "D":
        if not df.empty and df["time"].iloc[-1].date() == now.date() and now.hour * 60 + now.minute < 15 * 60 + 45:
            df = df.iloc[:-1]
    else:
        minutes = int(resolution)
        if not df.empty and df["time"].iloc[-1] + timedelta(minutes=minutes) > now:
            df = df.iloc[:-1]
    return df.reset_index(drop=True), provider


def get_candles(symbol: str, timeframe: str = "D", days: int = 120, as_of: str | None = None) -> dict[str, Any]:
    """Closed OHLCV candles (Asia/Kolkata). timeframe: D, 60 or 15. Provider is named in the result."""
    src = "fyers/yfinance"
    try:
        if timeframe not in _RES:
            return error(src, f"timeframe must be one of {list(_RES)}")
        df, provider = fetch_candles(symbol, timeframe, days, as_of)
        if df.empty:
            return unavailable(provider, f"no candles for {clean_symbol(symbol)}")
        data = [{"t": r.time.isoformat(), "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume} for r in df.itertuples()]
        return ok(provider, data, symbol=clean_symbol(symbol), timeframe=timeframe, count=len(data))
    except Exception as exc:  # noqa: BLE001 - envelope instead of crash
        return error(src, exc)


# ------------------------------------------------------------------ option chain
def _save_snapshot(symbol: str, row: dict[str, Any]) -> None:
    """One row per capture in SQLite (table oi_snapshots)."""
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO oi_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                    (clean_symbol(symbol), row["captured_at"], row["call_oi"], row["put_oi"], row.get("pcr"), row.get("ltp"),
                     json.dumps(row.get("support")), json.dumps(row.get("resistance")), row.get("max_pain")))


def _window_changes(symbol: str, call_oi: float, put_oi: float, as_of: datetime) -> dict[str, Any]:
    """Weekly/monthly OI change from our own saved snapshots (the broker gives only day-on-day)."""
    with connect() as con:
        df = pd.DataFrame([dict(r) for r in con.execute("SELECT captured_at, call_oi, put_oi FROM oi_snapshots WHERE symbol=? ORDER BY captured_at",
                                                        (clean_symbol(symbol),))])
    if df.empty:
        return {}
    df["captured_at"] = pd.to_datetime(df["captured_at"], utc=True)
    out: dict[str, Any] = {}
    for label, days in (("1w", 7), ("1m", 30)):
        cutoff = pd.Timestamp(as_of).tz_convert("UTC") - pd.Timedelta(days=days)
        old = df[df["captured_at"] <= cutoff]
        if old.empty:
            out[label] = "insufficient_history"
            continue
        r = old.iloc[-1]
        out[label] = {"from": str(r["captured_at"]), "call_oi_change": call_oi - float(r["call_oi"]), "put_oi_change": put_oi - float(r["put_oi"])}
    return out


def derive_levels(calls: list[dict[str, Any]], puts: list[dict[str, Any]], spot: float | None) -> dict[str, Any]:
    """Support / resistance from open interest. Resistance = highest call-OI strikes at or above spot, support = highest
    put-OI strikes at or below spot (strongest first). Max pain = strike where option writers' total payout is smallest
    (approximate: only the fetched strikes are used). OI levels are context: heavy OI can also be hedging."""
    oi = lambda r: float(r.get("oi") or 0)  # noqa: E731
    strike = lambda r: float(r.get("strike_price") or 0)  # noqa: E731
    up = [c for c in calls if spot is None or strike(c) >= spot] or calls
    dn = [p for p in puts if spot is None or strike(p) <= spot] or puts
    fmt = lambda r: {"strike": strike(r), "oi": oi(r), "oi_change": r.get("oich")}  # noqa: E731
    res = [fmt(r) for r in sorted(up, key=oi, reverse=True)[:3] if oi(r) > 0]
    sup = [fmt(r) for r in sorted(dn, key=oi, reverse=True)[:3] if oi(r) > 0]
    strikes = sorted({strike(r) for r in calls + puts})
    pain = {k: sum(max(0.0, k - strike(c)) * oi(c) for c in calls) + sum(max(0.0, strike(p) - k) * oi(p) for p in puts) for k in strikes}
    return {"resistance": res, "support": sup, "max_pain_approx": min(pain, key=pain.get) if pain else None,
            "basis": "open interest, nearest expiry, fetched strikes only"}


def summarize_chain(payload: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    """Pure function: FYERS options-chain-v3 payload -> compact summary. (Unit-tested with a fixture.)"""
    data = payload.get("data") or {}
    chain = data.get("optionsChain") or []
    under = next((c for c in chain if not c.get("option_type")), None)
    calls = [c for c in chain if c.get("option_type") == "CE"]
    puts = [c for c in chain if c.get("option_type") == "PE"]
    if not calls and not puts:
        raise SourceError("empty option chain")
    call_oi = sum(float(c.get("oi") or 0) for c in calls)
    put_oi = sum(float(c.get("oi") or 0) for c in puts)

    def top(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best = sorted(rows, key=lambda r: float(r.get("oi") or 0), reverse=True)[:3]
        return [{"strike": r.get("strike_price"), "oi": r.get("oi"), "oi_change": r.get("oich"), "volume": r.get("volume")} for r in best]

    return {
        "underlying_ltp": (under or {}).get("ltp"),
        "underlying_change_pct": (under or {}).get("ltpchp"),
        "expiries": [e.get("date") for e in (data.get("expiryData") or [])][:4],
        "total_call_oi": call_oi, "total_put_oi": put_oi,
        "pcr_oi": round(put_oi / call_oi, 3) if call_oi else None,
        "day_call_oi_change": sum(float(c.get("oich") or 0) for c in calls),
        "day_put_oi_change": sum(float(c.get("oich") or 0) for c in puts),
        "call_volume": sum(float(c.get("volume") or 0) for c in calls),
        "put_volume": sum(float(c.get("volume") or 0) for c in puts),
        "top_call_oi_strikes": top(calls), "top_put_oi_strikes": top(puts),
        "levels": derive_levels(calls, puts, (under or {}).get("ltp")),
        "note": "Chain covers nearest expiry and strikes around spot only.",
    }


def get_option_chain(symbol: str, strikecount: int = 10, as_of: str | None = None) -> dict[str, Any]:
    """FYERS option-chain summary (OI, OI change, PCR, volume, top strikes) + weekly/monthly OI change from saved snapshots.
    Returns status=unavailable for stocks without listed options."""
    src = "fyers_option_chain"
    try:
        s = get_settings()
        t = parse_as_of(as_of)
        if t.date() < now_ist().date():
            return unavailable(src, "historical option chains are not available (live snapshot only)")
        sym = clean_symbol(symbol)
        day = now_ist().date().isoformat()

        # Require configured credentials before returning either cached or fresh data.
        # This keeps the tool's contract deterministic when credentials are absent.
        if not (s.fyers_app_id and s.fyers_access_token):
            return unavailable(src, "FYERS credentials not set")

        if refreshed_today("option_chain", sym) and (cached := kv_get("option_chain", f"{sym}|{day}")):
            return ok(src, cached["summary"], symbol=sym, cache="hit", captured_at=cached["captured_at"],
                      note="Cached from the first load today (refresh-once-a-day rule); use --force-refresh for a live re-pull.")
        _throttle()
        resp = http_get(requests.Session(), FYERS_OPTION_CHAIN_URL, headers={"Authorization": f"{s.fyers_app_id}:{s.fyers_access_token}"},
                        params={"symbol": fyers_symbol(symbol), "strikecount": str(strikecount)})
        if resp.status_code in (401, 403):
            return error(src, f"FYERS authentication failed (HTTP {resp.status_code}: {resp.text[:200]}). "
                              "Token expired/invalid? Regenerate it with: python -m scripts.fyers_login")
        payload = resp.json()
        if payload.get("s") != "ok":
            return unavailable(src, f"no option chain: {payload.get('message') or payload.get('code')}")
        summary = summarize_chain(payload, t)
        summary["windows"] = _window_changes(symbol, summary["total_call_oi"], summary["total_put_oi"], t)
        stamp = now_ist().isoformat(timespec="seconds")
        lv = summary["levels"]
        _save_snapshot(symbol, {"captured_at": stamp, "call_oi": summary["total_call_oi"], "put_oi": summary["total_put_oi"],
                                "pcr": summary["pcr_oi"], "ltp": summary["underlying_ltp"], "support": lv["support"],
                                "resistance": lv["resistance"], "max_pain": lv["max_pain_approx"]})
        kv_set("option_chain", f"{sym}|{day}", {"summary": summary, "captured_at": stamp})
        mark_refreshed("option_chain", sym)
        return ok(src, summary, symbol=sym, cache="refreshed", captured_at=stamp)
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)
