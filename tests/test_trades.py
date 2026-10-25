"""Deterministic tests for the Trader trade plan (entry/exit/SL/quantity) and the trade-level simulation."""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd
import pytest

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


# --------------------------------------------------------------------------- pure exit / fill mechanics
def test_resolve_exit_outcomes():
    assert bt.resolve_exit([114, 120, 131], [112, 112, 125], [114, 118, 128], 0, "long", 108, 130) == ("target", 130, 2)
    assert bt.resolve_exit([114, 116], [112, 107], [114, 109], 0, "long", 108, 130) == ("stop", 108, 1)
    assert bt.resolve_exit([114, 131], [112, 107], [114, 120], 0, "long", 108, 130)[0] == "stop"   # straddle -> stop
    o = bt.resolve_exit([114, 118, 119, 120], [112, 112, 112, 113], [114, 116, 117, 118], 0, "long", 100, 200, max_bars=3)
    assert o[0] == "open" and o[2] == 3


def test_limit_fill_requires_touch():
    assert bt._limit_fill([114, 113], [112, 109], 0, "long", 110, 5, 1) == 1       # low 109 <= 110 -> filled
    assert bt._limit_fill([114, 113], [112, 111], 0, "long", 110, 5, 1) is None    # never traded down to 110


# --------------------------------------------------------------------------- realistic portfolio simulation
def _noisy(drift, n=320, seed=1, s="2022-01-03"):
    """Noisy geometric series so swing pivots (and therefore demand/supply zones) actually form."""
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(np.full(n, drift) + rng.normal(0, 0.012, n)))
    t = pd.date_range(s, periods=n, freq="B")
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"time": t, "open": o, "high": c * (1 + rng.uniform(0, 0.012, n)),
                         "low": c * (1 - rng.uniform(0, 0.012, n)), "close": c, "volume": rng.uniform(1e6, 2e6, n)})


def test_portfolio_is_realistic_and_self_consistent():
    panel = {f"S{i}": _noisy(d, seed=i) for i, d in enumerate([0.002, 0.0012, 0.0004, -0.0004, -0.0012, -0.002])}
    dates = [panel["S0"]["time"].iloc[i] for i in range(140, 280, 10)]
    recs = bt.run_trade_portfolio(panel, dates, capital=100000.0, risk_pct=1.0, max_bars=40, entry_mode="market")
    summ = bt.summarize_portfolio(recs, 100000.0)
    assert summ["n_trades"] == len(recs) >= 1
    assert set(summ) >= {"final_equity", "total_return_pct", "cagr_pct", "max_drawdown_pct", "profit_factor",
                         "win_rate", "expectancy_r"}
    # equity reconciles exactly with the sum of realised P&L, and drawdown is non-positive
    assert summ["final_equity"] == pytest.approx(100000.0 + sum(t["pnl"] for t in recs), abs=0.01)
    assert summ["max_drawdown_pct"] <= 0.0
    for t in recs:
        assert t["quantity"] > 0 and t["notional"] > 0 and t["bars_held"] >= 1
        assert (t["entry"] > t["stop_loss"]) if t["bias"] == "long" else (t["entry"] < t["stop_loss"])
    # at most one open position per symbol: next entry only after the previous trade on that symbol has exited
    by_sym = defaultdict(list)
    for t in recs:
        by_sym[t["symbol"]].append(t)
    for ts in by_sym.values():
        ts.sort(key=lambda x: x["entry_date"])
        for a, b in zip(ts, ts[1:]):
            assert b["entry_date"] >= a["exit_date"]


def test_portfolio_long_only_flag():
    panel = {f"S{i}": _noisy(d, seed=i) for i, d in enumerate([0.002, -0.002, 0.0012, -0.0012])}
    dates = [panel["S0"]["time"].iloc[i] for i in range(150, 260, 15)]
    recs = bt.run_trade_portfolio(panel, dates, capital=100000.0, max_bars=40, allow_short=False)
    assert all(t["bias"] == "long" for t in recs)
