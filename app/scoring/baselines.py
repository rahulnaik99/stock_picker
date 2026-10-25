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
