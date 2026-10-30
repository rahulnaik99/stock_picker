"""Deterministic per-agent baseline scores (-10..+10). Pure Python, unit-tested.

Each specialist LLM is given its baseline and may only ADJUST it within a small band (see base.MAX_ADJUST),
so the number is reproducible and auditable while the LLM contributes judgment the rubric cannot capture.
Every function returns (score, parts) where `parts` explains the contribution of each component.
"""
from __future__ import annotations

from typing import Any

LENDER_PREFIXES = ("bank", "nbfc", "housing_finance", "govt_financier")


def _clamp(x: float, lo: float = -10.0, hi: float = 10.0) -> float:
    return round(max(lo, min(hi, x)), 2)


def _bucket(pct: float | None, edges=(25, 10, 3, -3, -10, -25), points=(3, 2, 1, 0, -1, -2, -3)) -> float:
    """Map a % change to an ordinal score. >25->+3, 10..25->+2, 3..10->+1, -3..3->0, ... <-25->-3."""
    if pct is None:
        return 0.0
    for edge, pt in zip(edges, points):
        if pct >= edge:
            return float(pt)
    return float(points[-1])


# --------------------------------------------------------------------------- fundamental
def fundamental_baseline(metrics_block: dict[str, Any] | None, sub_sector: str | None = None) -> tuple[float, dict[str, Any]]:
    """From `quarterly_results.metrics`: PAT YoY + streak + net-margin trend. Lenders are capped without asset quality."""
    parts: dict[str, Any] = {}
    if not metrics_block:
        return 0.0, {"reason": "no quarterly metrics"}
    m = metrics_block.get("metrics", {})
    pat = m.get("pat", {})
    parts["pat_yoy"] = _bucket(pat.get("yoy_1"))                      # earnings growth, the dominant term
    streak = pat.get("streak_quarters") or 0
    parts["pat_streak"] = _clamp((1 if streak > 0 else -1 if streak < 0 else 0) * min(abs(streak), 2) * 0.5, -1, 1)
    nm = metrics_block.get("net_margin_pct", {})
    if isinstance(nm.get("latest"), (int, float)) and isinstance(nm.get("year_ago"), (int, float)):
        d = nm["latest"] - nm["year_ago"]
        parts["margin_trend"] = 0.5 if d > 1 else -0.5 if d < -1 else 0.0
    else:
        parts["margin_trend"] = 0.0
    score = sum(v for v in parts.values() if isinstance(v, (int, float)))
    if metrics_block.get("history_short"):
        score *= 0.7                                                 # thin history -> less conviction
        parts["history_short_damp"] = "x0.7"
    # Banks/NBFCs: can't be strongly bullish on earnings alone without GNPA/NNPA.
    if sub_sector and str(sub_sector).startswith(LENDER_PREFIXES):
        score = min(score, 3.0)
        parts["lender_cap"] = "<= +3 without asset quality"
    return _clamp(score), parts


# --------------------------------------------------------------------------- industry
def _macro(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next((r for r in (rows or []) if r.get("name") == name), {})


def industry_baseline(macro_rows: list[dict[str, Any]] | None) -> tuple[float, dict[str, Any]]:
    """Sector momentum + market stress from macro_snapshot (Nifty Bank / Nifty 50 momentum, India VIX)."""
    parts: dict[str, Any] = {}
    bank = _macro(macro_rows, "NIFTY_BANK")
    nifty = _macro(macro_rows, "NIFTY_50")
    vix = _macro(macro_rows, "INDIA_VIX")
    parts["nifty_bank_5d"] = _bucket(bank.get("chg_5d_pct"), edges=(3, 1, -1, -3), points=(2, 1, 0, -1, -2))
    parts["nifty_50_5d"] = _clamp(_bucket(nifty.get("chg_5d_pct"), edges=(3, 1, -1, -3), points=(1, 0.5, 0, -0.5, -1)), -1, 1)
    v5 = vix.get("chg_5d_pct")
    parts["vix_spike"] = -1.0 if isinstance(v5, (int, float)) and v5 > 20 else (-0.5 if isinstance(v5, (int, float)) and v5 > 8 else 0.0)
    return _clamp(sum(parts.values())), parts


# --------------------------------------------------------------------------- investment trend
def trend_baseline(fii_dii: dict[str, Any] | None, shareholding: list[dict[str, Any]] | None,
                   level: str) -> tuple[float, dict[str, Any]]:
    """Promoter-stake trend (stock-specific) + market-wide FII/DII net direction (small)."""
    parts: dict[str, Any] = {}
    data = (fii_dii or {}).get("data") if isinstance(fii_dii, dict) else None
    analytics = data.get("analytics", {}) if isinstance(data, dict) else {}

    def _net(cat: str) -> float:
        a = analytics.get(cat, {})
        for k in ("net_5d", "net_1d"):
            v = a.get(k)
            if isinstance(v, (int, float)):
                return v
        return 0.0
    net = _net("FII") + _net("DII")
    parts["market_flow"] = 1.0 if net > 0 else -1.0 if net < 0 else 0.0

    if shareholding and len(shareholding) >= 2:
        def _pr(row):
            try:
                return float(row.get("pr_and_prgrp"))
            except (TypeError, ValueError):
                return None
        newest, oldest = _pr(shareholding[0]), _pr(shareholding[-1])
        if newest is not None and oldest is not None:
            d = newest - oldest
            parts["promoter_trend"] = 2.0 if d > 0.5 else -2.0 if d < -0.5 else 0.0
    score = sum(v for v in parts.values() if isinstance(v, (int, float)))
    return _clamp(score, -5, 5), parts


# --------------------------------------------------------------------------- horizon divergence (LT vs ST fundamental)
def horizon_baseline_delta(horizon_view: dict[str, Any] | None) -> float:
    """Baseline adjustment from the long-term/short-term fundamental divergence (range ±2).
    Turnaround (LT bear + ST bull) and sustained growth give a positive nudge; slowdown and
    sustained decline give a negative one. Capped so the LLM can still steer within ±2."""
    if not horizon_view:
        return 0.0
    div = (horizon_view.get("divergence") or {})
    sig = div.get("signal", "mixed")
    delta = {"turnaround": 0.8, "sustained_growth": 1.0, "slowdown": -0.8, "sustained_decline": -1.0, "mixed": 0.0}
    d = delta.get(sig, 0.0)
    lt = (horizon_view.get("long_term_3y") or {})
    cagr = lt.get("pat_cagr_pct")
    if isinstance(cagr, (int, float)):
        if cagr >= 25:
            d += 0.4 if d >= 0 else -0.4
        elif cagr < 0 and sig in ("sustained_decline", "slowdown"):
            d -= 0.3
    return round(max(-2.0, min(2.0, d)), 2)


# --------------------------------------------------------------------------- trader (demand/supply, bull & bear)
def trader_baseline(zones: dict[str, Any] | None, event_ctx: dict[str, Any] | None = None,
                    option_ctx: dict[str, Any] | None = None,
                    fundamental_ctx: dict[str, Any] | None = None) -> tuple[float, dict[str, Any]]:
    """Price-action lens: BULL score vs BEAR score from zones + event + option + fundamental alignment.
    Returns (net_score, parts) where parts exposes bull_score and bear_score for the LLM to argue both sides."""
    if not zones:
        return 0.0, {"reason": "no zones"}
    pa = zones.get("price_action", {}) or {}
    nd, ns = zones.get("nearest_demand"), zones.get("nearest_supply")
    bull: dict[str, float] = {}
    bear: dict[str, float] = {}

    # 1) Sitting on a zone: a demand touch favours a bounce (bull); a supply touch favours rejection (bear).
    if pa.get("position") == "at_demand" and nd:
        bull["at_demand"] = round(1.5 + (nd.get("strength", 0) / 100) * 1.5, 2)
    elif pa.get("position") == "at_supply" and ns:
        bear["at_supply"] = round(1.5 + (ns.get("strength", 0) / 100) * 1.5, 2)

    # 2) EMA stack / VWAP side / recent structure.
    stack = zones.get("ema_stack")
    if stack == "bullish":
        bull["ema_stack"] = 1.5
    elif stack == "bearish":
        bear["ema_stack"] = 1.5
    side = zones.get("vwap_side")
    if side == "above":
        bull["vwap"] = 1.0
    elif side == "below":
        bear["vwap"] = 1.0
    if pa.get("last3_up") is True:
        bull["structure"] = 0.5
    elif pa.get("last3_up") is False:
        bear["structure"] = 0.5
    loc = pa.get("close_location")
    if isinstance(loc, (int, float)):
        if loc >= 0.6:
            bull["close_loc"] = 0.5
        elif loc <= 0.4:
            bear["close_loc"] = 0.5

    # 3) Overhead room vs downside cushion: a far supply (lots of room up) is mildly bullish and vice-versa.
    if ns and isinstance(ns.get("dist_pct"), (int, float)) and ns["dist_pct"] >= 6:
        bull["room_above"] = 0.5
    if nd and isinstance(nd.get("dist_pct"), (int, float)) and nd["dist_pct"] <= -6:
        bear["air_below"] = 0.5

    # 4) Multi-timeframe alignment (daily + 1h + 15m agreeing is a stronger read than a single frame).
    mtf = zones.get("mtf_bias")
    if mtf == "bullish_aligned":
        bull["mtf_aligned"] = 0.5
    elif mtf == "bearish_aligned":
        bear["mtf_aligned"] = 0.5

    # 5) Event alignment: upcoming catalysts + institutional flow (small terms, max ±0.6 total).
    if event_ctx:
        upcoming = event_ctx.get("upcoming_catalysts") or []
        fii = event_ctx.get("fii_dii") or {}
        deal_lean = event_ctx.get("deal_lean", "neutral")
        if any(e.get("type") == "dividend" for e in upcoming) and pos == "at_demand":
            bull["dividend_at_demand"] = 0.3
        fii_lean = fii.get("lean", "neutral")
        if fii_lean == "net_buying":
            bull["fii_flow"] = 0.2
        elif fii_lean == "net_selling":
            bear["fii_flow"] = 0.2
        if deal_lean == "net_buy":
            bull["block_deal_buy"] = 0.3
        elif deal_lean == "net_sell":
            bear["block_deal_sell"] = 0.3

    # 6) Option chain alignment: PCR tilt + OI confluence with zones (max ±0.7).
    if option_ctx and option_ctx.get("status") != "unavailable":
        pcr = option_ctx.get("pcr")
        conf = option_ctx.get("confluence") or {}
        pcr_lean = option_ctx.get("pcr_lean", "neutral")
        if pcr_lean == "put_heavy":                  # protective buyers = demand likely holds
            bull["option_pcr_put_heavy"] = 0.3
        elif pcr_lean == "call_heavy":               # speculative OTM calls = bearish lean
            bear["option_pcr_call_heavy"] = 0.3
        if conf.get("max_pain_at_demand"):           # price gravitates toward max pain
            bull["option_max_pain_demand"] = 0.25
        if conf.get("max_pain_at_supply"):
            bear["option_max_pain_supply"] = 0.25
        if conf.get("call_wall_confirms_supply"):    # call writers defending = supply is real
            bear["option_call_wall"] = 0.2
        if conf.get("put_floor_confirms_demand"):    # put writers protecting = demand is real
            bull["option_put_floor"] = 0.2
        quad = option_ctx.get("oi_quadrant", "mixed")
        if quad == "long_buildup":
            bull["option_oi_quadrant"] = 0.2
        elif quad == "short_buildup":
            bear["option_oi_quadrant"] = 0.2

    # 7) Fundamental alignment: earnings momentum + bank asset quality (max ±0.7).
    if fundamental_ctx and fundamental_ctx.get("status") != "unavailable":
        lean = fundamental_ctx.get("fundamental_lean", "stable")
        if lean == "improving":
            bull["fundamental_tailwind"] = 0.4
        elif lean == "deteriorating":
            bear["fundamental_headwind"] = 0.4
        bk = fundamental_ctx.get("bank_kpis") or {}
        gnpa = bk.get("gnpa_pct")
        if isinstance(gnpa, (int, float)):
            if gnpa < 2.0:
                bull["low_gnpa"] = 0.3    # clean book = fundamental confidence
            elif gnpa > 7.0:
                bear["high_gnpa"] = 0.3   # stress = cap bullish upside

    bull_score = round(sum(bull.values()), 2)
    bear_score = round(sum(bear.values()), 2)
    net = _clamp(bull_score - bear_score)
    return net, {"bull_score": bull_score, "bear_score": bear_score, "bull_parts": bull, "bear_parts": bear}

# --------------------------------------------------------------------------- relative strength
def rs_baseline(rs_data: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    """Stock's RS vs sector index. Leading + improving = bull; lagging + deteriorating = bear."""
    parts: dict[str, float] = {}
    if not rs_data:
        return 0.0, {"reason": "no RS data"}
    sig = rs_data.get("signal", "inline")
    trend = rs_data.get("rs_trend", "unknown")
    rs13 = (rs_data.get("rs_vs_sector_pct") or {}).get("13w") or 0
    parts["rs_signal"] = {"leading": 2.0, "outperforming": 1.0, "inline": 0.0,
                          "underperforming": -1.0, "lagging": -2.0}.get(sig, 0.0)
    parts["rs_trend"]  = {"improving": 0.5, "stable": 0.0, "deteriorating": -0.5}.get(trend, 0.0)
    # Magnitude boost for extreme RS
    if rs13 > 10:
        parts["rs_magnitude"] = 0.5
    elif rs13 < -10:
        parts["rs_magnitude"] = -0.5
    score = sum(parts.values())
    return _clamp(score, -4, 4), parts


# --------------------------------------------------------------------------- quant risk
def quant_risk_baseline(risk_data: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    """Risk profile adjustment: high-beta/deep-drawdown stocks need smaller position multiplier.
    Note: this baseline directly informs the position_size_factor rather than the direction score.
    It produces a SMALL score nudge (max ±1) — risk doesn't change direction, just conviction."""
    parts: dict[str, float] = {}
    if not risk_data:
        return 0.0, {"reason": "no risk data"}
    beta = risk_data.get("beta_vs_nifty")
    mdd1y = (risk_data.get("max_drawdown_pct") or {}).get("1y")
    rl = risk_data.get("risk_level", "medium")
    # High risk stocks: slight bearish nudge (harder to hold through volatility)
    parts["risk_level"] = {"low": 0.5, "medium": 0.0, "high": -0.5}.get(rl, 0.0)
    # Only let beta nudge the score when it is RELIABLE — an unreliable near-zero beta is a
    # data-alignment artifact (too few overlapping days) and must not be read as "defensive".
    if isinstance(beta, (int, float)) and risk_data.get("beta_reliable"):
        parts["beta_extreme"] = 0.0 if 0.7 <= beta <= 1.5 else (-0.3 if beta > 1.5 else 0.2)
    return _clamp(sum(parts.values()), -1, 1), parts


# --------------------------------------------------------------------------- promoter intelligence
def promoter_baseline(pledge_data: dict[str, Any] | None,
                      shareholding: list[dict[str, Any]] | None) -> tuple[float, dict[str, Any]]:
    """Pledge risk and promoter stake trend. Zero/falling pledge = bull tilt; high/rising = bear."""
    parts: dict[str, float] = {}
    if pledge_data and pledge_data.get("status") not in ("error", "unavailable"):
        risk = pledge_data.get("risk", "low")
        trend = pledge_data.get("trend", "stable")
        pct = pledge_data.get("latest_pledge_pct")
        parts["pledge_risk"] = {"low": 0.5, "caution": -0.3, "high": -1.5}.get(risk, 0.0)
        if trend == "falling": parts["pledge_improving"] = 0.3
        elif trend == "rising": parts["pledge_rising"] = -0.3
    # Promoter stake trend from shareholding (rising stake = skin-in-the-game)
    if shareholding and len(shareholding) >= 2:
        def _pr(r):
            try: return float(str(r.get("pr_and_prgrp") or 0).replace(",", ""))
            except (TypeError, ValueError): return None
        newest, oldest = _pr(shareholding[0]), _pr(shareholding[-1])
        if newest is not None and oldest is not None:
            d = newest - oldest
            if d > 0.5: parts["promoter_buying"]  =  0.4
            elif d < -0.5: parts["promoter_selling"] = -0.4
    score = sum(parts.values())
    return _clamp(score, -3, 3), parts


# --------------------------------------------------------------------------- catalyst quality
def catalyst_quality_baseline(catalyst_data: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    """Catalyst quality score from classified events/news.
    Maps the composite quality label to a small baseline adjustment (max ±1.5)."""
    if not catalyst_data:
        return 0.0, {"reason": "no catalyst data"}
    label = catalyst_data.get("composite_quality", "mixed")
    score_map = {"strong_positive": 1.5, "positive": 0.8, "mildly_positive": 0.3,
                 "mixed": 0.0, "mildly_negative": -0.3, "negative": -0.8, "strong_negative": -1.5}
    score = score_map.get(label, 0.0)
    return _clamp(score, -1.5, 1.5), {"quality_label": label, "score": score}


# --------------------------------------------------------------------------- option chain
def option_baseline(chain: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    """Price/OI quadrant + PCR tilt (day-on-day)."""
    parts: dict[str, Any] = {}
    if not chain:
        return 0.0, {"reason": "no chain"}
    dprice = chain.get("underlying_change_pct")
    doi = (chain.get("day_call_oi_change") or 0) + (chain.get("day_put_oi_change") or 0)
    if isinstance(dprice, (int, float)):
        if dprice > 0:
            parts["quadrant"] = 2.0 if doi > 0 else 1.0           # long build-up / short covering
        elif dprice < 0:
            parts["quadrant"] = -2.0 if doi > 0 else -1.0         # short build-up / long unwinding
        else:
            parts["quadrant"] = 0.0
    else:
        parts["quadrant"] = 0.0
    pcr = chain.get("pcr_oi")
    parts["pcr_tilt"] = 0.5 if isinstance(pcr, (int, float)) and pcr > 1.3 else (-0.5 if isinstance(pcr, (int, float)) and pcr < 0.7 else 0.0)
    return _clamp(sum(parts.values()), -5, 5), parts
