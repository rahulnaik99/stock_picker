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
