"""Deterministic technical indicators (pandas only). The LLM explains these numbers; it never computes them."""
from __future__ import annotations

import numpy as np
import pandas as pd


def to_frame(candles: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    return df.drop_duplicates("ts").sort_values("ts").set_index("dt")


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    rs = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def session_vwap(intraday: pd.DataFrame) -> float | None:
    """VWAP of the most recent session in an intraday frame (needs volume)."""
    if intraday is None or intraday.empty or intraday["volume"].sum() <= 0:
        return None
    last_day = intraday.index[-1].date()
    day = intraday[intraday.index.date == last_day]
    typical = (day["high"] + day["low"] + day["close"]) / 3
    vol = day["volume"].sum()
    return float((typical * day["volume"]).sum() / vol) if vol > 0 else None


def pivots(prev: pd.Series) -> dict:
    p = (prev["high"] + prev["low"] + prev["close"]) / 3
    return {"pivot": p, "r1": 2 * p - prev["low"], "s1": 2 * p - prev["high"],
            "r2": p + (prev["high"] - prev["low"]), "s2": p - (prev["high"] - prev["low"])}


def fibonacci(df: pd.DataFrame, lookback: int = 60) -> dict | None:
    w = df.tail(lookback)
    if len(w) < 10:
        return None
    hi, lo = float(w["high"].max()), float(w["low"].min())
    rng = hi - lo
    up_trend = w["high"].idxmax() > w["low"].idxmin()
    levels = {f"{r}": (hi - rng * r if up_trend else lo + rng * r) for r in (0.236, 0.382, 0.5, 0.618)}
    return {"swing_high": hi, "swing_low": lo, "direction": "up" if up_trend else "down", "retracements": levels}


def support_resistance(df: pd.DataFrame, window: int = 5, lookback: int = 120) -> dict:
    w = df.tail(lookback)
    close = float(w["close"].iloc[-1])
    highs = w["high"][(w["high"] == w["high"].rolling(2 * window + 1, center=True).max())]
    lows = w["low"][(w["low"] == w["low"].rolling(2 * window + 1, center=True).min())]
    res = sorted(float(x) for x in highs if x > close)
    sup = sorted((float(x) for x in lows if x < close), reverse=True)
    return {"nearest_resistance": res[0] if res else None, "nearest_support": sup[0] if sup else None}


def market_structure(df: pd.DataFrame, bars: int = 20) -> str:
    w = df.tail(bars)
    if len(w) < bars:
        return "insufficient_data"
    h1, h2 = w["high"].iloc[: bars // 2].max(), w["high"].iloc[bars // 2:].max()
    l1, l2 = w["low"].iloc[: bars // 2].min(), w["low"].iloc[bars // 2:].min()
    if h2 > h1 and l2 > l1:
        return "higher_highs_higher_lows"
    if h2 < h1 and l2 < l1:
        return "lower_highs_lower_lows"
    return "range_or_mixed"


def compute_features(daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> dict:
    """Features from COMPLETED daily candles (+ optional intraday for VWAP)."""
    if len(daily) < 30:
        raise ValueError(f"need >=30 daily candles, got {len(daily)}")
    c = daily["close"]
    last = float(c.iloc[-1])
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    a14 = float(atr(daily).iloc[-1])
    r14 = float(rsi(c).iloc[-1])
    ad = adx(daily)
    vol20 = daily["volume"].tail(20).mean()
    hi52, lo52 = float(daily["high"].tail(252).max()), float(daily["low"].tail(252).min())

    def dist(e):  # % distance of price from an average (None until the average exists)
        v = e.iloc[-1]
        return None if pd.isna(v) else round((last / float(v) - 1) * 100, 2)

    vwap = session_vwap(intraday) if intraday is not None else None
    extended = bool(r14 > 75 or (not pd.isna(e20.iloc[-1]) and last > float(e20.iloc[-1]) + 2.5 * a14))
    out = {
        "last_close": round(last, 2), "last_candle": str(daily.index[-1].date()), "candles_used": len(daily),
        "ema20": _r(e20.iloc[-1]), "ema50": _r(e50.iloc[-1]), "ema200": _r(e200.iloc[-1]),
        "pct_from_ema20": dist(e20), "pct_from_ema50": dist(e50), "pct_from_ema200": dist(e200),
        "rsi14": round(r14, 1), "atr14": round(a14, 2), "atr_pct": round(a14 / last * 100, 2),
        "adx14": _r(ad.iloc[-1]), "volume_ratio_20d": round(float(daily["volume"].iloc[-1] / vol20), 2) if vol20 else None,
        "pct_from_52w_high": round((last / hi52 - 1) * 100, 2), "pct_from_52w_low": round((last / lo52 - 1) * 100, 2),
        "market_structure": market_structure(daily), "support_resistance": support_resistance(daily),
        "pivots": {k: round(float(v), 2) for k, v in pivots(daily.iloc[-2]).items()}, "fibonacci": fibonacci(daily),
        "vwap_last_session": _r(vwap), "price_vs_vwap_pct": round((last / vwap - 1) * 100, 2) if vwap else None,
        "return_5d_pct": round((last / float(c.iloc[-6]) - 1) * 100, 2) if len(c) > 6 else None,
        "return_20d_pct": round((last / float(c.iloc[-21]) - 1) * 100, 2) if len(c) > 21 else None,
        "looks_extended": extended,
    }
    return out


def _r(v) -> float | None:
    return None if v is None or pd.isna(v) else round(float(v), 2)
