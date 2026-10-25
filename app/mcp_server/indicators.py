"""Deterministic technical indicators (pandas). The LLM explains these numbers; it never invents them.

Input frames: columns ts (tz-aware, IST), open, high, low, close, volume - oldest first.
Only COMPLETED candles are used (the in-progress bar at `as_of` is dropped).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.mcp_server.providers.common import IST, Unavailable

MARKET_CLOSE = time(15, 30)


# ----------------------------------------------------------------------------- primitives
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    au, ad = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean(), dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(ad != 0, 100.0).where(au.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    sig = line.ewm(span=9, adjust=False).mean()
    return line, sig, line - sig


def session_vwap(intraday: pd.DataFrame) -> float | None:
    """VWAP of the latest session in the intraday frame (typical price x volume)."""
    if intraday is None or intraday.empty:
        return None
    last_day = intraday["ts"].dt.date.iloc[-1]
    d = intraday[intraday["ts"].dt.date == last_day]
    vol = d["volume"].sum()
    if vol <= 0:
        return None
    tp = (d["high"] + d["low"] + d["close"]) / 3
    return float((tp * d["volume"]).sum() / vol)


def classic_pivots(prev: pd.Series) -> dict[str, float]:
    h, l, c = float(prev["high"]), float(prev["low"]), float(prev["close"])
    p = (h + l + c) / 3
    return {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + (h - l), "S2": p - (h - l)}


def fibonacci(df: pd.DataFrame, lookback: int = 60) -> dict[str, float] | None:
    w = df.tail(lookback)
    hi, lo = float(w["high"].max()), float(w["low"].min())
    if hi <= lo:
        return None
    rng = hi - lo
    return {"swing_high": hi, "swing_low": lo, **{f"fib_{int(r * 1000)/10:g}": hi - r * rng for r in (0.236, 0.382, 0.5, 0.618)}}


def swing_levels(df: pd.DataFrame, close: float, window: int = 5, lookback: int = 120) -> dict[str, float | None]:
    w = df.tail(lookback).reset_index(drop=True)
    highs, lows = [], []
    for i in range(window, len(w) - window):
        seg = w.iloc[i - window:i + window + 1]
        if w["high"][i] == seg["high"].max():
            highs.append(float(w["high"][i]))
        if w["low"][i] == seg["low"].min():
            lows.append(float(w["low"][i]))
    above = sorted(h for h in highs if h > close)
    below = sorted((l for l in lows if l < close), reverse=True)
    return {"nearest_resistance": above[0] if above else None, "nearest_support": below[0] if below else None}


# ----------------------------------------------------------------------------- completed-candle filter
def drop_incomplete(df: pd.DataFrame, as_of: datetime, *, intraday_minutes: int | None = None) -> pd.DataFrame:
    """Remove bars not complete at `as_of`, and anything after it (point-in-time)."""
    if df is None or df.empty:
        return df
    as_of = as_of.astimezone(IST)
    if intraday_minutes:
        done = df["ts"] + timedelta(minutes=intraday_minutes) <= as_of
        return df[done].reset_index(drop=True)
    df = df[df["ts"].dt.date <= as_of.date()]
    if not df.empty and df["ts"].dt.date.iloc[-1] == as_of.date() and as_of.time() < MARKET_CLOSE:
        df = df.iloc[:-1]
    return df.reset_index(drop=True)


# ----------------------------------------------------------------------------- pack
def _f(x: Any, nd: int = 2) -> float | None:
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def build_pack(daily: pd.DataFrame, intraday: pd.DataFrame | None, as_of: datetime, *, intraday_minutes: int = 15) -> dict:
    daily = drop_incomplete(daily, as_of)
    intraday = drop_incomplete(intraday, as_of, intraday_minutes=intraday_minutes) if intraday is not None else None
    if daily is None or len(daily) < 60:
        raise Unavailable(f"need >=60 completed daily candles, got {0 if daily is None else len(daily)}")

    c, v = daily["close"], daily["volume"]
    last = float(c.iloc[-1])
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    r, a, ax = rsi(c), atr(daily), adx(daily)
    m_line, m_sig, m_hist = macd(c)
    vol_ratio = float(v.iloc[-1] / v.tail(20).mean()) if v.tail(20).mean() > 0 else None
    vwap = session_vwap(intraday) if intraday is not None else None

    def pct(n: int) -> float | None:
        return _f((last / float(c.iloc[-1 - n]) - 1) * 100) if len(c) > n else None

    def dist(e: pd.Series) -> float | None:
        return None if np.isnan(e.iloc[-1]) else _f((last / float(e.iloc[-1]) - 1) * 100)

    atr_now = float(a.iloc[-1]) if not np.isnan(a.iloc[-1]) else None
    ext_atr = _f((last - float(e20.iloc[-1])) / atr_now) if atr_now and not np.isnan(e20.iloc[-1]) else None
    rsi_now = _f(r.iloc[-1])
    overextended = bool((ext_atr is not None and ext_atr > 2.5) or (rsi_now is not None and rsi_now > 75))

    e20n, e50n, e200n = (float(x.iloc[-1]) if not np.isnan(x.iloc[-1]) else None for x in (e20, e50, e200))
    if e200n and last > e20n > e50n > e200n:
        trend = "strong_uptrend"
    elif e50n and last > e50n and e20n > e50n:
        trend = "uptrend"
    elif e200n and last < e20n < e50n < e200n:
        trend = "strong_downtrend"
    elif e50n and last < e50n and e20n < e50n:
        trend = "downtrend"
    else:
        trend = "sideways"

    hi52, lo52 = float(daily.tail(252)["high"].max()), float(daily.tail(252)["low"].min())
    pack = {
        "last_close": _f(last),
        "daily_bars": len(daily),
        "data_as_of": daily["ts"].iloc[-1].isoformat(),
        "trend_state": trend,
        "ema": {"20": _f(e20n), "50": _f(e50n), "200": _f(e200n)},
        "pct_from_ema": {"20": dist(e20), "50": dist(e50), "200": dist(e200)},
        "rsi14": rsi_now,
        "adx14": _f(ax.iloc[-1]),
        "atr14": _f(atr_now),
        "macd": {"line": _f(m_line.iloc[-1], 3), "signal": _f(m_sig.iloc[-1], 3), "hist": _f(m_hist.iloc[-1], 3),
                 "hist_rising": bool(m_hist.iloc[-1] > m_hist.iloc[-2])},
        "volume_ratio_20d": _f(vol_ratio),
        "returns_pct": {"5d": pct(5), "20d": pct(20), "60d": pct(60)},
        "range_52w": {"high": _f(hi52), "low": _f(lo52), "pct_from_high": _f((last / hi52 - 1) * 100),
                      "pct_from_low": _f((last / lo52 - 1) * 100)},
        "extension_atr_from_ema20": ext_atr,
        "overextended": overextended,
        "vwap_last_session": _f(vwap),
        "price_vs_vwap_pct": _f((last / vwap - 1) * 100) if vwap else None,
        "vwap_note": None if vwap else "intraday data unavailable: VWAP not computed",
        "pivots_prev_session": {k: _f(x) for k, x in classic_pivots(daily.iloc[-1]).items()},
        "fibonacci_60d": {k: _f(x) for k, x in (fibonacci(daily) or {}).items()},
        "levels": {k: _f(x) for k, x in swing_levels(daily, last).items()},
    }
    pack["rule_score"], pack["rule_score_parts"] = rule_score(pack, last, float(vwap) if vwap else None)
    return pack


def rule_score(p: dict, last: float, vwap: float | None) -> tuple[float, dict[str, float]]:
    """Transparent reference score (-10..+10). The Indicator agent sees it but makes its own call."""
    parts: dict[str, float] = {}
    e = p["ema"]
    t = 0.0
    if e["20"]: t += 1 if last > e["20"] else -1
    if e["20"] and e["50"]: t += 1 if e["20"] > e["50"] else -1
    if e["200"]: t += 1 if last > e["200"] else -1
    if (p["adx14"] or 0) > 25: t *= 1.3
    parts["trend"] = round(t, 2)
    rs = p["rsi14"]
    if rs is not None:
        parts["rsi"] = 2.0 if 55 <= rs < 70 else 1.0 if 70 <= rs < 80 else -1.0 if rs >= 80 else 0.0 if rs >= 45 \
            else -2.0 if rs >= 30 else -1.0
    parts["macd"] = 1.0 if (p["macd"]["hist"] or 0) > 0 and p["macd"]["hist_rising"] else -1.0 if (p["macd"]["hist"] or 0) < 0 else 0.0
    vr, r5 = p["volume_ratio_20d"], (p["returns_pct"]["5d"] or 0)
    parts["volume"] = (1.0 if r5 > 0 else -1.0) if vr and vr > 1.3 else 0.0
    if vwap:
        parts["vwap"] = 1.0 if last > vwap else -1.0
    if p["overextended"]:
        parts["extension_penalty"] = -1.5
    return round(max(-10.0, min(10.0, sum(parts.values()))), 2), parts
