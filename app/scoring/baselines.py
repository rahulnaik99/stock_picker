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
def trader_baseline(zones: dict[str, Any] | None, event_ctx: dict[str, Any] | None = None) -> tuple[float, dict[str, Any]]:
    """Price-action lens: builds a BULL score (demand/EMA/VWAP/structure in the long's favour) and a BEAR score
    (supply/EMA/VWAP/structure in the short's favour) from `get_demand_supply_zones`, then nets them.
    Optional `event_ctx` adds small alignment terms from upcoming catalysts, block/bulk deals and FII/DII flow.
    `parts` keeps `bull_score` and `bear_score` so the agent can argue both sides explicitly."""
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

        # Dividend near a demand zone = support buying ahead of ex-date (mild bull)
        if any(e.get("type") == "dividend" for e in upcoming):
            if pos == "at_demand":
                bull["dividend_at_demand"] = 0.3

        # FII/DII net flow (market-wide — applied conservatively as a small tilt only)
        fii_lean = fii.get("lean", "neutral")
        if fii_lean == "net_buying":
            bull["fii_flow"] = 0.2
        elif fii_lean == "net_selling":
            bear["fii_flow"] = 0.2

        # Block/bulk deal activity (stock-specific — stronger signal than market-wide FII)
        if deal_lean == "net_buy":
            bull["block_deal_buy"] = 0.3
        elif deal_lean == "net_sell":
            bear["block_deal_sell"] = 0.3

    bull_score = round(sum(bull.values()), 2)
    bear_score = round(sum(bear.values()), 2)
    net = _clamp(bull_score - bear_score)
    return net, {"bull_score": bull_score, "bear_score": bear_score, "bull_parts": bull, "bear_parts": bear}


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
