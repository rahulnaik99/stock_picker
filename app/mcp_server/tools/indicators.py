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


# ===================================================================================================
# Demand / supply zones - the price-action "Trader" lens (deterministic; the LLM only argues around it)
# ===================================================================================================
ZONE_LOOKBACK = 140
ZONE_WING = 3


def _pivots(d: pd.DataFrame, wing: int) -> tuple[list[tuple[int, float, float]], list[tuple[int, float, float]]]:
    """Swing pivot highs (supply candidates) and lows (demand candidates) as (bar_index, price, volume)."""
    highs, lows = [], []
    h, lo, vol = d["high"].to_numpy(), d["low"].to_numpy(), d["volume"].to_numpy()
    for i in range(wing, len(d) - wing):
        if h[i] == h[i - wing: i + wing + 1].max():
            highs.append((i, float(h[i]), float(vol[i])))
        if lo[i] == lo[i - wing: i + wing + 1].min():
            lows.append((i, float(lo[i]), float(vol[i])))
    return highs, lows


def _cluster(pivots: list[tuple[int, float, float]], tol: float, med_vol: float, n_bars: int) -> list[dict[str, Any]]:
    """Merge pivots whose prices sit within `tol` into zones; summarise touches / volume / recency."""
    zones: list[dict[str, Any]] = []
    for idx, price, vol in sorted(pivots, key=lambda x: x[1]):
        z = next((z for z in zones if abs(price - z["_mid"]) <= tol), None)
        if z is None:
            zones.append({"_p": [price], "_i": [idx], "_v": [vol], "_mid": price})
        else:
            z["_p"].append(price); z["_i"].append(idx); z["_v"].append(vol)
            z["_mid"] = sum(z["_p"]) / len(z["_p"])
    out = []
    for z in zones:
        lo, hi = min(z["_p"]), max(z["_p"])
        vr = (sum(z["_v"]) / len(z["_v"]) / med_vol) if med_vol else 1.0
        out.append({"low": round(lo, 2), "high": round(hi, 2), "mid": round((lo + hi) / 2, 2),
                    "touches": len(z["_p"]), "avg_vol_ratio": round(vr, 2),
                    "recency_frac": round(max(z["_i"]) / max(n_bars - 1, 1), 2)})
    return out


def _zone_strength(z: dict[str, Any]) -> float:
    """0-100: more touches, higher volume, more recent -> stronger (confluence adds on top, in the caller)."""
    s = min(z["touches"], 4) * 15 + min(z["avg_vol_ratio"], 2.0) * 10 + z["recency_frac"] * 20
    return round(min(s, 100.0), 0)


def demand_supply_zones(daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> dict[str, Any]:
    """Pure function: demand (support) and supply (resistance) zones plus the EMA stack, session VWAP and simple
    price-action context the Trader agent reasons about. Needs >= 60 daily candles."""
    if len(daily) < 60:
        raise ValueError(f"need >= 60 daily candles, got {len(daily)}")
    c = daily["close"]
    px = float(c.iloc[-1])
    atr_v = float(atr(daily).iloc[-1])
    last = lambda s: None if pd.isna(s.iloc[-1]) else round(float(s.iloc[-1]), 2)  # noqa: E731
    e20 = last(c.ewm(span=20, adjust=False).mean())
    e50 = last(c.ewm(span=50, adjust=False).mean())
    e200 = last(c.ewm(span=200, adjust=False).mean()) if len(daily) >= 200 else None
    vwap = None if intraday is None or intraday.empty else session_vwap(intraday)

    d = daily.tail(ZONE_LOOKBACK).reset_index(drop=True)
    med_vol = float(d["volume"].median()) or 1.0
    tol = max(atr_v, px * 0.01)
    highs, lows = _pivots(d, ZONE_WING)
    supply = _cluster(highs, tol, med_vol, len(d))
    demand = _cluster(lows, tol, med_vol, len(d))

    confl_levels = [("EMA20", e20), ("EMA50", e50), ("EMA200", e200), ("VWAP", vwap)]
    for z in demand + supply:
        conf = [name for name, val in confl_levels if val is not None and z["low"] <= val <= z["high"]]
        z["confluence"] = conf
        z["dist_pct"] = round((z["mid"] / px - 1) * 100, 2)   # +above price / -below price
        z["strength"] = round(min(100.0, _zone_strength(z) + 10 * len(conf)), 0)

    demand = sorted([z for z in demand if z["mid"] <= px * 1.02], key=lambda z: abs(px - z["mid"]))[:4]
    supply = sorted([z for z in supply if z["mid"] >= px * 0.98], key=lambda z: abs(z["mid"] - px))[:4]
    nd = demand[0] if demand else None
    ns = supply[0] if supply else None

    def _inside(z: dict[str, Any] | None) -> bool:
        return bool(z and (z["low"] - 0.5 * atr_v) <= px <= (z["high"] + 0.5 * atr_v))
    position = "at_demand" if _inside(nd) else "at_supply" if _inside(ns) else "mid_range"
    bar = daily.iloc[-1]
    rng = float(bar["high"] - bar["low"])
    close_loc = round((float(bar["close"] - bar["low"]) / rng), 2) if rng > 0 else 0.5  # 1=closed at high
    last3_up = bool(c.iloc[-1] > c.iloc[-3]) if len(c) >= 3 else None

    ema_stack = ("bullish" if e20 and e50 and px > e20 > e50 else
                 "bearish" if e20 and e50 and px < e20 < e50 else "mixed")
    vwap_side = None if vwap is None else ("above" if px > vwap else "below")
    # Volume context: is recent activity expanding or contracting vs the lookback median?
    recent5 = float(d["volume"].tail(5).mean()) if len(d) >= 5 else med_vol
    vol_ratio = round(recent5 / med_vol, 2) if med_vol else 1.0
    vol_trend = "expanding" if vol_ratio > 1.25 else "contracting" if vol_ratio < 0.75 else "normal"
    last_bar = d.iloc[-1]
    high_vol_last_bar = bool(float(last_bar["volume"]) > med_vol * 1.5)

    return {
        "as_of_bar": daily["time"].iloc[-1].isoformat(), "close": round(px, 2), "atr14": round(atr_v, 2),
        "ema20": e20, "ema50": e50, "ema200": e200, "ema_stack": ema_stack,
        "vwap_session": None if vwap is None else round(vwap, 2), "vwap_side": vwap_side,
        "demand_zones": demand, "supply_zones": supply,
        "nearest_demand": nd, "nearest_supply": ns,
        "price_action": {"position": position, "last3_up": last3_up, "close_location": close_loc,
                         "reclaim_ema20": bool(e20 and px > e20), "lost_ema20": bool(e20 and px < e20)},
        "volume_context": {"recent5_avg": round(recent5, 0), "median_lookback": round(med_vol, 0),
                           "ratio": vol_ratio, "trend": vol_trend,
                           "high_volume_last_bar": high_vol_last_bar,
                           "note": "expanding volume on a bounce from demand = conviction; on a rejection from supply = conviction. Contracting volume on a move = weak conviction."},
    }


def _tf_bias(z: dict[str, Any]) -> str:
    """One timeframe's directional bias from its EMA stack / zone position: 'bull' | 'bear' | 'neutral'."""
    stack = z.get("ema_stack")
    pos = (z.get("price_action") or {}).get("position")
    if stack == "bullish":
        return "bull"
    if stack == "bearish":
        return "bear"
    if pos == "at_demand":
        return "bull"
    if pos == "at_supply":
        return "bear"
    return "neutral"


def _compact_tf(z: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim a timeframe's zone read for the MTF payload (keep the decision-relevant fields only)."""
    if not z:
        return None
    return {"ema_stack": z.get("ema_stack"), "bias": _tf_bias(z),
            "position": (z.get("price_action") or {}).get("position"),
            "nearest_demand": z.get("nearest_demand"), "nearest_supply": z.get("nearest_supply")}


def build_trade_plan(z: dict[str, Any], capital: float = 100000.0, risk_pct: float = 1.0,
                     force_bias: str | None = None) -> dict[str, Any]:
    """Deterministic trade plan from the (daily) zone read. Picks a side, then sets entry at the reaction zone,
    stop-loss just beyond it (0.5*ATR buffer), target at the opposite zone (or a 2R projection if none), and sizes
    the position so the loss at the stop = capital * risk_pct%. Pure and auditable; the LLM only justifies it.
    `force_bias` ("long" | "short") bypasses the automatic side selection — used to produce BOTH a long and a
    short plan from the same zones (e.g. to generate the bull/bear dual chart)."""
    px, atr_v = z.get("close"), z.get("atr14") or 0.0
    nd, ns = z.get("nearest_demand"), z.get("nearest_supply")
    stack = z.get("ema_stack")
    pos = (z.get("price_action") or {}).get("position")
    if px is None:
        return {"bias": "none", "reason": "no price"}
    buf = max(0.5 * atr_v, px * 0.003)

    if force_bias == "long":
        bias = "long" if nd is not None else "none"
        if bias == "none":
            return {"bias": "none", "reason": "no demand zone available for a long plan"}
    elif force_bias == "short":
        bias = "short" if ns is not None else "none"
        if bias == "none":
            return {"bias": "none", "reason": "no supply zone available for a short plan"}
    else:
        long_ok = nd is not None and (stack == "bullish" or pos == "at_demand")
        short_ok = ns is not None and (stack == "bearish" or pos == "at_supply")
        bias = ("long" if long_ok and not short_ok else "short" if short_ok and not long_ok
                else "long" if long_ok and stack == "bullish" else "short" if short_ok and stack == "bearish" else "none")
        if bias == "none":
            return {"bias": "none", "reason": "no clean demand/supply setup for a directional trade"}

    if bias == "long":
        entry = round(px if pos == "at_demand" else nd["high"], 2)
        stop = round(nd["low"] - buf, 2)
        target = round(ns["low"] if ns else entry + 2 * (entry - stop), 2)
        per_share_risk = round(entry - stop, 2)
        reward = round(target - entry, 2)
    else:
        entry = round(px if pos == "at_supply" else ns["low"], 2)
        stop = round(ns["high"] + buf, 2)
        target = round(nd["high"] if nd else entry - 2 * (stop - entry), 2)
        per_share_risk = round(stop - entry, 2)
        reward = round(entry - target, 2)

    if per_share_risk <= 0:
        return {"bias": "none", "reason": "degenerate stop from zone geometry (no positive risk per share)"}
    rr = round(reward / per_share_risk, 2)
    risk_amount = round(capital * risk_pct / 100.0, 2)
    quantity = int(risk_amount // per_share_risk)
    flags = []
    if rr < 1.0:
        flags.append("reward_to_risk_below_1")
    if quantity <= 0:
        flags.append("risk_budget_too_small_for_one_share")
    return {
        "bias": bias, "entry": entry, "stop_loss": stop, "target": target, "risk_reward": rr,
        "per_share_risk": per_share_risk, "reward_per_share": reward, "risk_amount": risk_amount,
        "quantity": quantity, "notional": round(quantity * entry, 2), "capital": capital, "risk_pct": risk_pct,
        "basis": {"demand": nd, "supply": ns, "ema_stack": stack, "position": pos,
                  "atr14": round(atr_v, 2), "stop_buffer": round(buf, 2)},
        "flags": flags,
    }


def get_demand_supply_zones(symbol: str, as_of: str | None = None,
                            capital: float = 100000.0, risk_pct: float = 1.0) -> dict[str, Any]:
    """Multi-timeframe demand/supply zones (daily + 1h + 15m) with an MTF bias, EMA stack, session VWAP, price
    action AND a deterministic trade plan (entry / stop-loss / target / risk-reward / position size). Feeds the
    bull/bear Trader agent. `capital` and `risk_pct` drive position sizing (risk per trade = capital * risk_pct%)."""
    try:
        daily, provider = fetch_candles(symbol, "D", 420, as_of)
        if daily.empty:
            return unavailable(provider, f"no daily candles for {clean_symbol(symbol)}")
        frames: dict[str, pd.DataFrame | None] = {"1d": daily}
        for label, res, days in (("1h", "60", 75), ("15m", "15", 14)):
            try:
                frames[label], _ = fetch_candles(symbol, res, days, as_of)
            except Exception:  # noqa: BLE001 - a missing intraday frame only drops that timeframe
                frames[label] = None
        intraday = frames.get("15m")
        data = demand_supply_zones(daily, intraday if intraday is not None and len(intraday) else None)

        # Per-timeframe zone reads + MTF alignment (higher timeframe = structure, lower = timing).
        tfz: dict[str, dict[str, Any] | None] = {"1d": data}
        for label in ("1h", "15m"):
            df = frames.get(label)
            try:
                tfz[label] = demand_supply_zones(df) if df is not None and len(df) >= 60 else None
            except Exception:  # noqa: BLE001
                tfz[label] = None
        biases = {k: _tf_bias(v) for k, v in tfz.items() if v}
        vals = set(biases.values())
        alignment = ("bullish_aligned" if vals == {"bull"} else "bearish_aligned" if vals == {"bear"}
                     else "conflicting" if ("bull" in vals and "bear" in vals) else "mixed")
        data["timeframes"] = {k: _compact_tf(v) for k, v in tfz.items()}
        data["mtf"] = {"bias": biases, "alignment": alignment}
        data["mtf_bias"] = alignment

        data["trade_plan"] = build_trade_plan(data, capital=capital, risk_pct=risk_pct)
        data["long_plan"] = build_trade_plan(data, capital=capital, risk_pct=risk_pct, force_bias="long")
        data["short_plan"] = build_trade_plan(data, capital=capital, risk_pct=risk_pct, force_bias="short")
        data["candle_provider"] = provider
        data["bars_used"] = int(len(daily))
        return ok(provider, data, symbol=clean_symbol(symbol))
    except Exception as exc:  # noqa: BLE001
        return error("demand_supply_zones", exc)


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
