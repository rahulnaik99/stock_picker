"""Deterministic tests for the Trader trade plan (entry/exit/SL/quantity) and the trade-level simulation."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest import trades as bt
from app.mcp_server.tools import indicators


# --------------------------------------------------------------------------- trade plan math
def _long_zones():
    return {"close": 116.0, "atr14": 4.0, "ema_stack": "bullish", "price_action": {"position": "mid_range"},
            "nearest_demand": {"low": 110.0, "high": 114.0, "mid": 112.0, "strength": 80, "dist_pct": -1.7},
            "nearest_supply": {"low": 130.0, "high": 132.0, "mid": 131.0, "strength": 60, "dist_pct": 12.0}}


def test_build_trade_plan_long_math():
    p = indicators.build_trade_plan(_long_zones(), capital=100000.0, risk_pct=1.0)
    assert p["bias"] == "long"
    assert p["entry"] == 114.0 and p["stop_loss"] == 108.0 and p["target"] == 130.0   # buffer = max(0.5*ATR, .3%) = 2.0
    assert p["per_share_risk"] == 6.0 and p["reward_per_share"] == 16.0
    assert p["risk_reward"] == round(16 / 6, 2)
    assert p["risk_amount"] == 1000.0 and p["quantity"] == 166        # floor(1000 / 6)
    assert p["notional"] == round(166 * 114.0, 2) and p["flags"] == []


def test_build_trade_plan_short_math():
    z = {"close": 116.0, "atr14": 4.0, "ema_stack": "bearish", "price_action": {"position": "mid_range"},
         "nearest_demand": {"low": 100.0, "high": 104.0, "mid": 102.0, "strength": 50, "dist_pct": -10.0},
         "nearest_supply": {"low": 118.0, "high": 122.0, "mid": 120.0, "strength": 80, "dist_pct": 3.0}}
    p = indicators.build_trade_plan(z, capital=100000.0, risk_pct=1.0)
    assert p["bias"] == "short"
    assert p["entry"] == 118.0 and p["stop_loss"] == 124.0 and p["target"] == 104.0
    assert p["per_share_risk"] == 6.0 and p["risk_reward"] == round(14 / 6, 2) and p["quantity"] == 166


def test_build_trade_plan_at_demand_uses_price_and_2r_projection_when_no_supply():
    z = {"close": 112.5, "atr14": 4.0, "ema_stack": "mixed", "price_action": {"position": "at_demand"},
         "nearest_demand": {"low": 110.0, "high": 114.0, "mid": 112.0, "strength": 70, "dist_pct": -0.4},
         "nearest_supply": None}
    p = indicators.build_trade_plan(z)
    assert p["bias"] == "long" and p["entry"] == 112.5 and p["stop_loss"] == 108.0   # 110 - 2.0
    # no supply -> target is a 2R projection: entry + 2*(entry - stop)
    assert p["target"] == round(112.5 + 2 * (112.5 - 108.0), 2)


def test_build_trade_plan_none_when_no_clean_setup():
    z = {"close": 116.0, "atr14": 4.0, "ema_stack": "mixed", "price_action": {"position": "mid_range"},
         "nearest_demand": {"low": 110.0, "high": 114.0, "mid": 112.0, "strength": 40, "dist_pct": -2.0},
         "nearest_supply": {"low": 130.0, "high": 132.0, "mid": 131.0, "strength": 40, "dist_pct": 12.0}}
    assert indicators.build_trade_plan(z)["bias"] == "none"


def test_quantity_scales_with_risk_budget():
    z = _long_zones()                       # per-share risk = 6.0
    assert indicators.build_trade_plan(z, capital=500000.0, risk_pct=1.0)["quantity"] == int(5000 // 6)
    assert indicators.build_trade_plan(z, capital=100000.0, risk_pct=2.0)["quantity"] == int(2000 // 6)


# --------------------------------------------------------------------------- simulation
def _plan():
    return indicators.build_trade_plan(_long_zones())   # entry 114, stop 108, target 130, qty 166, psr 6


def test_simulate_trade_target_hit():
    highs = [114, 120, 131]; lows = [112, 112, 125]; closes = [114, 118, 128]
    s = bt.simulate_trade(highs, lows, closes, 0, _plan())
    assert s["outcome"] == "target" and s["exit_price"] == 130.0 and s["bars_held"] == 2
    assert s["pnl_per_share"] == 16.0 and s["pnl"] == round(16 * 166, 2) and s["r_multiple"] == round(16 / 6, 3)


def test_simulate_trade_stop_hit():
    highs = [114, 116]; lows = [112, 107]; closes = [114, 109]
    s = bt.simulate_trade(highs, lows, closes, 0, _plan())
    assert s["outcome"] == "stop" and s["exit_price"] == 108.0
    assert s["pnl_per_share"] == -6.0 and s["pnl"] == round(-6 * 166, 2) and s["r_multiple"] == -1.0


def test_simulate_trade_straddle_bar_counts_as_stop():
    highs = [114, 131]; lows = [112, 107]; closes = [114, 120]   # one bar hits both -> stop first (conservative)
    assert bt.simulate_trade(highs, lows, closes, 0, _plan())["outcome"] == "stop"


def test_simulate_trade_open_at_cap():
    highs = [114, 118, 119, 120]; lows = [112, 112, 112, 113]; closes = [114, 116, 117, 118]
    s = bt.simulate_trade(highs, lows, closes, 0, _plan(), max_bars=3)
    assert s["outcome"] == "open" and s["exit_price"] == 118.0 and s["bars_held"] == 3


def test_simulate_trade_none_when_not_tradable():
    assert bt.simulate_trade([1], [1], [1], 0, {"bias": "none"}) is None
    assert bt.simulate_trade([1], [1], [1], 0, {"bias": "long", "quantity": 0}) is None


# --------------------------------------------------------------------------- end-to-end on a synthetic panel
def _trend(drift, n=320, seed=1, s="2022-01-03"):
    """Noisy geometric series so swing pivots (and therefore demand/supply zones) actually form."""
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(np.full(n, drift) + rng.normal(0, 0.01, n)))
    t = pd.date_range(s, periods=n, freq="B")
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"time": t, "open": o, "high": c * (1 + rng.uniform(0, 0.01, n)),
                         "low": c * (1 - rng.uniform(0, 0.01, n)), "close": c, "volume": rng.uniform(1e6, 2e6, n)})


def test_run_trade_backtest_and_summary_shapes():
    panel = {"UP": _trend(0.0015, seed=1), "DN": _trend(-0.0015, seed=2)}
    dates = [panel["UP"]["time"].iloc[i] for i in range(150, 260, 20)]
    recs = bt.run_trade_backtest(panel, dates, capital=100000.0, risk_pct=1.0, max_bars=40)
    summ = bt.summarize_trades(recs)
    assert summ["n_trades"] == len(recs) >= 1
    for r in recs:
        assert r["bias"] in ("long", "short") and r["quantity"] > 0 and r["outcome"] in ("target", "stop", "open")
        assert (r["entry"] > r["stop_loss"]) if r["bias"] == "long" else (r["entry"] < r["stop_loss"])
    assert set(summ) >= {"win_rate", "expectancy_r", "total_pnl", "by_side", "avg_risk_reward"}
