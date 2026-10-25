"""Deterministic technical indicators. The LLM only interprets these numbers; it never computes them."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.mcp_server.tools.common import clean_symbol, error, ok, unavailable
from app.mcp_server.tools.market import fetch_candles


def _wilder(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, down = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(up.notna())


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return _wilder(tr, n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, down = df["high"].diff(), -df["low"].diff()
    plus = np.where((up > down) & (up > 0), up, 0.0)
    minus = np.where((down > up) & (down > 0), down, 0.0)
    a = atr(df, n)
    pdi = 100 * _wilder(pd.Series(plus, index=df.index), n) / a
    mdi = 100 * _wilder(pd.Series(minus, index=df.index), n) / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return _wilder(dx.fillna(0), n)


def session_vwap(intraday: pd.DataFrame) -> float | None:
    """VWAP of the latest session only (needs intraday bars with volume)."""
    if intraday.empty:
        return None
    last_day = intraday["time"].iloc[-1].date()
    s = intraday[intraday["time"].dt.date == last_day]
    vol = s["volume"].sum()
    if vol <= 0:
        return None
    return float(((s["high"] + s["low"] + s["close"]) / 3 * s["volume"]).sum() / vol)


def swing_levels(df: pd.DataFrame, lookback: int = 60, wing: int = 3) -> dict[str, list[float]]:
    """Support/resistance from swing highs/lows in the last `lookback` bars, nearest to price first."""
    d = df.tail(lookback).reset_index(drop=True)
    highs, lows = [], []
    for i in range(wing, len(d) - wing):
        if d["high"][i] == d["high"][i - wing: i + wing + 1].max():
            highs.append(float(d["high"][i]))
        if d["low"][i] == d["low"][i - wing: i + wing + 1].min():
            lows.append(float(d["low"][i]))
    px = float(df["close"].iloc[-1])
    res = sorted({round(h, 2) for h in highs if h > px})[:3]
    sup = sorted({round(l, 2) for l in lows if l < px}, reverse=True)[:3]
    return {"resistance": res, "support": sup}


def compute(daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> dict[str, Any]:
    """Pure function used by the tool and the unit tests."""
    if len(daily) < 60:
        raise ValueError(f"need >= 60 daily candles, got {len(daily)}")
    c, v = daily["close"], daily["volume"]
    ema = {n: c.ewm(span=n, adjust=False).mean() for n in (20, 50, 200)}
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    r, a, x = rsi(c), atr(daily), adx(daily)
    px = float(c.iloc[-1])
    last = lambda s: None if pd.isna(s.iloc[-1]) else round(float(s.iloc[-1]), 4)  # noqa: E731
    e20, e50 = last(ema[20]), last(ema[50])
    e200 = last(ema[200]) if len(daily) >= 200 else None
    vol_ratio = float(v.iloc[-1] / v.tail(20).mean()) if v.tail(20).mean() > 0 else None
    prev = daily.iloc[-2]
    pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
    hi, lo = float(daily["high"].tail(60).max()), float(daily["low"].tail(60).min())
    hh = daily["high"].tail(10).max() > daily["high"].iloc[-20:-10].max()
    hl = daily["low"].tail(10).min() > daily["low"].iloc[-20:-10].min()
    structure = "higher_highs_higher_lows" if hh and hl else "lower_highs_lower_lows" if (not hh and not hl) else "mixed"
    atr_v = last(a)
    rsi_v = last(r)
    extended = bool((rsi_v is not None and rsi_v >= 75) or (atr_v and e20 and (px - e20) / atr_v >= 3))

    out = {
        "as_of_bar": daily["time"].iloc[-1].isoformat(), "close": round(px, 2),
        "ema20": e20, "ema50": e50, "ema200": e200,
        "dist_ema20_pct": round((px / e20 - 1) * 100, 2) if e20 else None,
        "dist_ema50_pct": round((px / e50 - 1) * 100, 2) if e50 else None,
        "dist_ema200_pct": round((px / e200 - 1) * 100, 2) if e200 else None,
        "rsi14": rsi_v, "adx14": last(x), "atr14": atr_v,
        "macd": last(macd), "macd_signal": last(macd_sig), "macd_hist": round(float(macd.iloc[-1] - macd_sig.iloc[-1]), 4),
        "volume_ratio_20d": round(vol_ratio, 2) if vol_ratio else None,
        "structure": structure, **swing_levels(daily),
        "pivot": round(float(pivot), 2), "r1": round(float(2 * pivot - prev["low"]), 2), "s1": round(float(2 * pivot - prev["high"]), 2),
        "fib_60d": {k: round(hi - (hi - lo) * f, 2) for k, f in (("38.2", .382), ("50", .5), ("61.8", .618))},
        "high_52w": round(float(daily["high"].tail(250).max()), 2), "low_52w": round(float(daily["low"].tail(250).min()), 2),
        "vwap_session": None if intraday is None else (None if (vw := session_vwap(intraday)) is None else round(vw, 2)),
        "extended": extended,
    }
    out["rule_score"], out["rule_score_parts"] = rule_score(out)
    return out


def rule_score(i: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """Transparent baseline in [-10, +10] so the LLM has a reference. Not a trading signal."""
    parts: dict[str, float] = {}
    px, e20, e50, e200 = i["close"], i["ema20"], i["ema50"], i["ema200"]
    stack = [px > e20, e20 > e50] + ([e50 > e200] if e200 else [])
    parts["trend_stack"] = {3: 4.0, 2: 2.0, 1: -2.0, 0: -4.0}.get(sum(stack), 0.0) if e200 else {2: 3.0, 1: 0.0, 0: -3.0}[sum(stack)]
    adx_v = i["adx14"] or 0
    parts["trend_strength"] = round(float(np.sign(parts["trend_stack"])) * (1.0 if adx_v >= 25 else 0.0), 1)
    rsi_v = i["rsi14"] or 50
    parts["momentum"] = 1.5 if 50 <= rsi_v < 70 else 0.5 if 70 <= rsi_v < 75 else -1.5 if rsi_v < 40 else 0.0 if rsi_v < 50 else -0.5
    parts["macd"] = 1.0 if i["macd_hist"] > 0 else -1.0
    parts["structure"] = {"higher_highs_higher_lows": 1.5, "lower_highs_lower_lows": -1.5}.get(i["structure"], 0.0)
    vr = i["volume_ratio_20d"] or 1
    parts["volume"] = 0.0 if vr < 1.2 else (1.0 if parts["trend_stack"] > 0 else -1.0)
    total = sum(parts.values())
    return round(max(-10.0, min(10.0, total)), 1), parts


MIN_BARS_PER_FRAME = 30


def timeframe_summary(df: pd.DataFrame) -> dict[str, Any] | None:
    """EMA 20/50/200 and RSI14 for one timeframe's closed candles (EMA200 only with >= 200 bars). None when history is too short."""
    if df is None or len(df) < MIN_BARS_PER_FRAME:
        return None
    c = df["close"]
    px = float(c.iloc[-1])
    ema = {n: float(c.ewm(span=n, adjust=False).mean().iloc[-1]) for n in (20, 50, 200) if len(df) >= (200 if n == 200 else n)}
    r = rsi(c).iloc[-1]
    e20, e50 = ema.get(20), ema.get(50)
    trend = ("up" if e20 and e50 and px > e20 > e50 else "down" if e20 and e50 and px < e20 < e50 else "mixed")
    return {"bars": int(len(df)), "last_bar": df["time"].iloc[-1].isoformat(), "close": round(px, 2),
            "ema20": round(e20, 2) if e20 else None, "ema50": round(e50, 2) if e50 else None,
            "ema200": round(ema[200], 2) if 200 in ema else None,
            "rsi14": None if pd.isna(r) else round(float(r), 1),
            "dist_ema20_pct": round((px / e20 - 1) * 100, 2) if e20 else None, "trend": trend}


def mtf_alignment(frames: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """Do 15m, 1h and daily agree? Pure function. A missing timeframe is reported, never treated as neutral."""
    have = {k: v for k, v in frames.items() if v}
    missing = [k for k, v in frames.items() if not v]
    if len(have) < 2:
        return {"alignment": "insufficient_timeframes", "missing": missing}
    trends = {k: v["trend"] for k, v in have.items()}
    rsi_zone = {k: ("overbought" if (v["rsi14"] or 50) >= 70 else "oversold" if (v["rsi14"] or 50) <= 30 else "neutral") for k, v in have.items()}
    if all(t == "up" for t in trends.values()):
        al = "bullish_aligned"
    elif all(t == "down" for t in trends.values()):
        al = "bearish_aligned"
    else:
        al = "conflicting"
    return {"alignment": al, "trends": trends, "rsi_zones": rsi_zone, "missing": missing}


def get_technical_indicators(symbol: str, as_of: str | None = None) -> dict[str, Any]:
    """Daily EMA20/50/200, RSI, ADX, ATR, MACD, levels, pivots, Fibonacci, volume ratio, `extended` flag and a `rule_score`, plus
    EMA/RSI on 15-minute, 1-hour and daily candles (`mtf`) and whether the timeframes agree. All computed in Python from closed candles."""
    try:
        daily, provider = fetch_candles(symbol, "D", 420, as_of)
        if daily.empty:
            return unavailable(provider, f"no daily candles for {clean_symbol(symbol)}")
        frames: dict[str, pd.DataFrame | None] = {"1d": daily}
        providers = {"1d": provider}
        for label, res, days in (("1h", "60", 75), ("15m", "15", 14)):
            try:
                frames[label], providers[label] = fetch_candles(symbol, res, days, as_of)
            except Exception:  # noqa: BLE001 - a missing intraday frame lowers completeness, it does not fail the tool
                frames[label], providers[label] = None, None
        intraday = frames.get("15m")
        data = compute(daily, intraday if intraday is not None and len(intraday) else None)
        data["candle_provider"] = provider
        data["bars_used"] = int(len(daily))
        mtf = {k: timeframe_summary(frames.get(k)) for k in ("15m", "1h", "1d")}
        data["mtf"] = {**mtf, **mtf_alignment(mtf), "providers": providers}
        bonus = {"bullish_aligned": 1.0, "bearish_aligned": -1.0}.get(data["mtf"]["alignment"], 0.0)
        data["rule_score_parts"]["mtf_alignment"] = bonus
        data["rule_score"] = round(max(-10.0, min(10.0, data["rule_score"] + bonus)), 1)
        if data["vwap_session"] is None:
            data["vwap_note"] = "VWAP unavailable (no intraday volume)"
        return ok(provider, data, symbol=clean_symbol(symbol))
    except Exception as exc:  # noqa: BLE001
        return error("indicators", exc)
