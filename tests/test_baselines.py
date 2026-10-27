"""Deterministic per-agent baseline rubrics. Pure, offline."""
from __future__ import annotations

from app.scoring import baselines


def test_fundamental_baseline_rewards_earnings_and_caps_lenders():
    block = {"history_short": True, "metrics": {"pat": {"yoy_1": 62.15, "streak_quarters": 1}},
             "net_margin_pct": {"latest": 30.55, "year_ago": 16.43}}
    # pat_yoy +3, streak +0.5, margin +0.5 = 4.0; history_short x0.7 = 2.8; lender cap leaves it (<=3).
    assert baselines.fundamental_baseline(block, "bank_psu")[0] == 2.8
    assert baselines.fundamental_baseline(None, "bank_psu")[0] == 0.0


def test_industry_baseline_reads_momentum_and_vix():
    macro = [{"name": "NIFTY_BANK", "chg_5d_pct": -2.73}, {"name": "NIFTY_50", "chg_5d_pct": -1.73},
             {"name": "INDIA_VIX", "chg_5d_pct": 20.17}]
    # nifty_bank -1, nifty_50 -0.5, vix spike -1 = -2.5
    assert baselines.industry_baseline(macro)[0] == -2.5
    assert baselines.industry_baseline([])[0] == 0.0


def test_option_baseline_quadrant():
    down_unwind = {"underlying_change_pct": -3.95, "day_call_oi_change": -9.2e6, "day_put_oi_change": -11.1e6, "pcr_oi": 0.896}
    assert baselines.option_baseline(down_unwind)[0] == -1.0   # price down + OI down = long unwinding
    up_build = {"underlying_change_pct": 1.2, "day_call_oi_change": 5e5, "day_put_oi_change": 5e5, "pcr_oi": 1.4}
    assert baselines.option_baseline(up_build)[0] == 2.5       # +2 quadrant +0.5 put-heavy tilt


def test_trend_baseline_promoter_and_market_flow():
    fii = {"data": {"analytics": {"FII": {"net_1d": 887.65}, "DII": {"net_1d": 3235.36}}}}
    flat = [{"pr_and_prgrp": "62.93"}, {"pr_and_prgrp": "62.93"}]
    assert baselines.trend_baseline(fii, flat, "stock_specific")[0] == 1.0           # market flow + only
    rising = [{"pr_and_prgrp": "65.0"}, {"pr_and_prgrp": "60.0"}]
    assert baselines.trend_baseline(fii, rising, "stock_specific")[0] == 3.0          # +1 flow +2 promoter
    # a list 'data' (as some fakes return) must not crash -> 0 market flow
    assert baselines.trend_baseline({"data": []}, None, "market_wide_only")[0] == 0.0


def test_trader_baseline_bull_vs_bear_and_net():
    # bull: sitting on a strong demand zone, bullish stack, above VWAP, strong up close
    bull_zones = {
        "ema_stack": "bullish", "vwap_side": "above",
        "nearest_demand": {"low": 114, "high": 116, "strength": 80, "dist_pct": -0.5},
        "nearest_supply": {"low": 125, "high": 126, "strength": 50, "dist_pct": 8.0},
        "price_action": {"position": "at_demand", "last3_up": True, "close_location": 0.8},
    }
    net, parts = baselines.trader_baseline(bull_zones)
    # at_demand 1.5+0.8*1.5=2.7, ema 1.5, vwap 1.0, struct 0.5, close 0.5, room_above 0.5 = 6.7 bull, 0 bear
    assert parts["bull_score"] == 6.7 and parts["bear_score"] == 0.0 and net == 6.7

    # bear: rejecting a strong supply zone, bearish stack, below VWAP, weak close
    bear_zones = {
        "ema_stack": "bearish", "vwap_side": "below",
        "nearest_demand": {"low": 100, "high": 102, "strength": 40, "dist_pct": -9.0},
        "nearest_supply": {"low": 116, "high": 118, "strength": 90, "dist_pct": 0.5},
        "price_action": {"position": "at_supply", "last3_up": False, "close_location": 0.2},
    }
    net2, parts2 = baselines.trader_baseline(bear_zones)
    assert parts2["bear_score"] > parts2["bull_score"] and net2 < 0
    assert baselines.trader_baseline(None)[0] == 0.0


def test_horizon_baseline_delta_signals():
    # turnaround: LT bear + ST bull -> positive delta
    hv = {"long_term_3y": {"trend": "deteriorating", "pat_cagr_pct": -5.0},
          "short_term_3q": {"trend": "improving"},
          "divergence": {"signal": "turnaround", "lt_trend": "deteriorating", "st_trend": "improving"}}
    assert baselines.horizon_baseline_delta(hv) > 0
    # sustained_decline: both bear -> negative
    hv2 = {"long_term_3y": {"trend": "deteriorating", "pat_cagr_pct": -18.0},
            "short_term_3q": {"trend": "deteriorating"},
            "divergence": {"signal": "sustained_decline", "lt_trend": "deteriorating", "st_trend": "deteriorating"}}
    assert baselines.horizon_baseline_delta(hv2) < 0
    # high-growth sustained: delta boosted beyond base
    hv3 = {"long_term_3y": {"trend": "improving", "pat_cagr_pct": 30.0},
            "short_term_3q": {"trend": "improving"},
            "divergence": {"signal": "sustained_growth", "lt_trend": "improving", "st_trend": "improving"}}
    assert baselines.horizon_baseline_delta(hv3) > baselines.horizon_baseline_delta(
        {"long_term_3y": {"trend": "improving", "pat_cagr_pct": 5.0},
         "short_term_3q": {"trend": "improving"},
         "divergence": {"signal": "sustained_growth", "lt_trend": "improving", "st_trend": "improving"}})
    assert baselines.horizon_baseline_delta(None) == 0.0


def test_rs_baseline():
    assert baselines.rs_baseline({"signal": "leading", "rs_trend": "improving", "rs_vs_sector_pct": {"13w": 12}})[0] > 2
    assert baselines.rs_baseline({"signal": "lagging",  "rs_trend": "deteriorating", "rs_vs_sector_pct": {"13w": -12}})[0] < -2
    assert baselines.rs_baseline({"signal": "inline",   "rs_trend": "stable", "rs_vs_sector_pct": {}})[0] == 0.0
    assert baselines.rs_baseline(None)[0] == 0.0


def test_quant_risk_baseline():
    low  = baselines.quant_risk_baseline({"risk_level": "low",  "beta_vs_nifty": 0.6})[0]
    high = baselines.quant_risk_baseline({"risk_level": "high", "beta_vs_nifty": 2.0})[0]
    assert low > high and -1 <= low <= 1 and -1 <= high <= 1


def test_promoter_baseline():
    zero_pledge = {"risk": "low", "trend": "stable", "latest_pledge_pct": 0.0}
    high_pledge = {"risk": "high", "trend": "rising", "latest_pledge_pct": 55.0}
    assert baselines.promoter_baseline(zero_pledge, None)[0] > 0
    assert baselines.promoter_baseline(high_pledge, None)[0] < 0


def test_catalyst_quality_baseline():
    assert baselines.catalyst_quality_baseline({"composite_quality": "strong_positive"})[0] > 1.0
    assert baselines.catalyst_quality_baseline({"composite_quality": "strong_negative"})[0] < -1.0
    assert baselines.catalyst_quality_baseline({"composite_quality": "mixed"})[0] == 0.0
