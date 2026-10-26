"""The technical chart renderer: produces PNGs from candles + zones + trade plans (no network, headless)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.mcp_server.tools import indicators


def _panel(drift: float, n: int = 220, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(np.full(n, drift) + rng.normal(0, 0.012, n)))
    t = pd.date_range("2023-01-02", periods=n, freq="B")
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"time": t, "open": o, "high": c * (1 + rng.uniform(0, 0.012, n)),
                         "low": c * (1 - rng.uniform(0, 0.012, n)), "close": c, "volume": rng.uniform(1e6, 2e6, n)})


def test_plot_trade_chart_writes_png(tmp_path):
    from app.viz.chart import plot_trade_chart
    df = _panel(0.0015)
    zones = indicators.demand_supply_zones(df.tail(420), None)
    plan = indicators.build_trade_plan(zones)
    out = tmp_path / "chart.png"
    path = plot_trade_chart(df, zones, plan, str(out), title="TEST")
    assert path == str(out) and out.exists() and out.stat().st_size > 2000


def test_plot_trade_chart_handles_no_plan(tmp_path):
    from app.viz.chart import plot_trade_chart
    df = _panel(0.0)
    zones = indicators.demand_supply_zones(df.tail(420), None)
    out = tmp_path / "noplan.png"
    plot_trade_chart(df, zones, {"bias": "none"}, str(out), title="FLAT")
    assert out.exists() and out.stat().st_size > 2000


def test_plot_dual_chart_writes_wide_png(tmp_path):
    from app.viz.chart import plot_dual_chart
    df = _panel(0.0012, seed=2)
    zones = indicators.demand_supply_zones(df.tail(420), None)
    long_plan = indicators.build_trade_plan(zones, force_bias="long")
    short_plan = indicators.build_trade_plan(zones, force_bias="short")
    out = tmp_path / "dual.png"
    plot_dual_chart(df, zones, long_plan, short_plan, str(out), symbol="TEST")
    assert out.exists() and out.stat().st_size > 5000  # dual chart is larger than single


def test_build_trade_plan_force_bias():
    df = _panel(0.0)
    zones = indicators.demand_supply_zones(df.tail(420), None)
    lp = indicators.build_trade_plan(zones, force_bias="long")
    sp = indicators.build_trade_plan(zones, force_bias="short")
    if zones.get("nearest_demand"):
        assert lp["bias"] == "long"
        assert lp["entry"] > lp["stop_loss"]   # long: entry above stop
    if zones.get("nearest_supply"):
        assert sp["bias"] == "short"
        assert sp["entry"] < sp["stop_loss"]   # short: entry below stop
