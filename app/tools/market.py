"""Price-based tools: technical features (FYERS first, yfinance fallback) and option-chain OI (FYERS)."""
from __future__ import annotations

import csv
import threading
from datetime import timedelta

import pandas as pd

from app.service.settings import get_settings
from app.tools import fyers, indicators
from app.tools.common import IST, envelope, log, parse_as_of, require_current, unavailable

_oi_lock = threading.Lock()


def _yahoo_daily(symbol: str, end, days: int) -> list[list[float]]:
    import yfinance as yf

    df = yf.download(f"{symbol}.NS", start=(end - timedelta(days=days)).date(), end=(end + timedelta(days=1)).date(),
                     interval="1d", auto_adjust=False, progress=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no rows")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return [[int(ix.timestamp()), r["Open"], r["High"], r["Low"], r["Close"], r["Volume"]] for ix, r in df.iterrows()]


def get_technical_features(symbol: str, as_of: str = "") -> dict:
    """Deterministic indicators (EMA20/50/200, RSI, ATR, ADX, VWAP, volume ratio, pivots, fib, support/resistance)
    computed from completed daily candles. Provider is FYERS; Yahoo is used only if FYERS fails (and is labelled)."""
    end = parse_as_of(as_of)
    provider, notes, daily_rows = "fyers", [], None
    try:
        daily_rows = fyers.history(symbol, "D", end - timedelta(days=500), end)
    except Exception as exc:
        notes.append(f"FYERS failed: {exc}")
        log.warning("FYERS daily failed for %s: %s", symbol, exc)
    if not daily_rows:
        try:
            daily_rows, provider = _yahoo_daily(symbol, end, 500), "yfinance"
            notes.append("Fell back to Yahoo Finance (unofficial, may differ from exchange data)")
        except Exception as exc:
            return unavailable(provider, "; ".join(notes + [f"yfinance failed: {exc}"]), as_of)
    daily = indicators.to_frame(daily_rows)
    daily = daily[daily.index <= end]
    if len(daily) and daily.index[-1].date() == end.date() and end.hour * 60 + end.minute < 15 * 60 + 45 and not as_of:
        daily = daily.iloc[:-1]  # drop the still-forming candle
        notes.append("dropped incomplete current-session daily candle")
    intraday = None
    if provider == "fyers":
        try:
            intraday = indicators.to_frame(fyers.history(symbol, "15", end - timedelta(days=7), end))
        except Exception as exc:
            notes.append(f"VWAP unavailable (intraday fetch failed: {exc})")
    try:
        feats = indicators.compute_features(daily, intraday)
    except ValueError as exc:
        return unavailable(provider, str(exc), as_of)
    stale_days = (end.date() - daily.index[-1].date()).days
    status = "stale" if stale_days > 5 else "ok"
    return envelope(status, provider, feats, "; ".join(notes) or None, as_of, timeframe="1D (VWAP: latest 15m session)")


# ---- option chain -------------------------------------------------------------------------------------------------
def _oi_path():
    p = get_settings().output_dir / "oi_snapshots.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _store_and_compare(symbol: str, day: str, ce: float, pe: float) -> dict:
    """Persist today's total OI; compare with the closest snapshots ~5 and ~20 days earlier (if we have them)."""
    path, rows = _oi_path(), []
    with _oi_lock:
        if path.exists():
            with open(path, newline="") as fh:
                rows = list(csv.DictReader(fh))
        rows = [r for r in rows if not (r["symbol"] == symbol and r["date"] == day)] + [
            {"date": day, "symbol": symbol, "ce_oi": ce, "pe_oi": pe}]
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "symbol", "ce_oi", "pe_oi"])
            w.writeheader()
            w.writerows(rows)
    hist = sorted((r for r in rows if r["symbol"] == symbol and r["date"] < day), key=lambda r: r["date"])
    out = {}
    for label, days in (("weekly", 5), ("monthly", 20)):
        target = (pd.Timestamp(day) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        older = [r for r in hist if r["date"] <= target]
        if older:
            o = older[-1]
            out[label] = {"vs_date": o["date"], "ce_oi_change_pct": _pct(ce, float(o["ce_oi"])), "pe_oi_change_pct": _pct(pe, float(o["pe_oi"]))}
        else:
            out[label] = None  # not enough stored history yet; never filled with zeros
    return out


def _pct(new: float, old: float) -> float | None:
    return round((new / old - 1) * 100, 2) if old else None


def summarize_chain(raw: dict) -> dict:
    chain = raw.get("optionsChain") or []
    opts = [r for r in chain if r.get("option_type") in ("CE", "PE")]
    if not opts:
        raise ValueError("no option rows in response")
    under = next((r for r in chain if r.get("option_type") not in ("CE", "PE")), {})

    def agg(kind, key):
        return float(sum((r.get(key) or 0) for r in opts if r["option_type"] == kind))

    ce_oi, pe_oi = agg("CE", "oi"), agg("PE", "oi")
    top = lambda kind, key: [{"strike": r.get("strike_price"), key: r.get(key)} for r in
                             sorted((r for r in opts if r["option_type"] == kind), key=lambda r: -(r.get(key) or 0))[:3]]
    exp = (raw.get("expiryData") or [{}])[0]
    return {
        "underlying_ltp": under.get("ltp"), "underlying_change_pct": under.get("ltpchp"), "nearest_expiry": exp.get("date"),
        "strikes_covered": len({r.get("strike_price") for r in opts}),
        "total_call_oi": ce_oi, "total_put_oi": pe_oi, "pcr_oi": round(pe_oi / ce_oi, 2) if ce_oi else None,
        "pcr_volume": round(agg("PE", "volume") / agg("CE", "volume"), 2) if agg("CE", "volume") else None,
        "call_oi_change_today": agg("CE", "oich"), "put_oi_change_today": agg("PE", "oich"),
        "top_call_oi_strikes": top("CE", "oi"), "top_put_oi_strikes": top("PE", "oi"),
        "top_call_oi_build_strikes": top("CE", "oich"), "top_put_oi_build_strikes": top("PE", "oich"),
        "india_vix": (raw.get("indiavixData") or {}).get("ltp"),
    }


def get_option_chain_oi(symbol: str, as_of: str = "") -> dict:
    """Option-chain OI summary from FYERS: PCR, OI change (today; weekly/monthly from our stored snapshots), key strikes.
    Stocks without listed options return status=unavailable (not a neutral score)."""
    why = require_current(as_of, "FYERS option chain")
    if why:
        return unavailable("fyers", why, as_of)
    try:
        raw = fyers.option_chain(symbol, strikecount=10)
        summary = summarize_chain(raw)
    except Exception as exc:
        return unavailable("fyers", f"option chain unavailable (stock may not have F&O, or API error): {exc}", as_of)
    day = parse_as_of(as_of).strftime("%Y-%m-%d")
    summary["oi_change_vs_stored_snapshots"] = _store_and_compare(symbol, day, summary["total_call_oi"], summary["total_put_oi"])
    return envelope("ok", "fyers", summary, None, as_of, caveat="OI covers the strikes fetched around spot; hedging vs directional intent cannot be separated")
