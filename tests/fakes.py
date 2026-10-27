"""Synthetic tool results for offline tests and scripts/dry_run.py. No network, deterministic per symbol."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.mcp_server.tools import indicators
from app.mcp_server.tools.common import IST, error, ok, unavailable

FNO = {"SBIN", "HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK"}
BROKEN = {"BROKENCO"}


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.encode()).hexdigest(), 16) % (2**32)


def synthetic_daily(symbol: str, n: int = 320, drift: float | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(_seed(symbol))
    d = (rng.uniform(-0.0015, 0.003) if drift is None else drift)
    close = 500 * np.exp(np.cumsum(rng.normal(d, 0.012, n)))
    high, low = close * (1 + rng.uniform(0, 0.012, n)), close * (1 - rng.uniform(0, 0.012, n))
    t = pd.date_range(end=datetime(2026, 10, 20, 15, 30, tzinfo=IST), periods=n, freq="B")
    return pd.DataFrame({"time": t, "open": close, "high": high, "low": low, "close": close, "volume": rng.uniform(1e6, 3e6, n)})


def fake_indicators(symbol: str, as_of: str | None = None) -> dict[str, Any]:
    if symbol in BROKEN:
        raise RuntimeError("synthetic failure")
    return ok("fake", {**indicators.compute(synthetic_daily(symbol)), "candle_provider": "fake", "bars_used": 320}, symbol=symbol)


def fake_zones(symbol: str, as_of: str | None = None, capital: float = 100000.0, risk_pct: float = 1.0) -> dict[str, Any]:
    if symbol in BROKEN:
        raise RuntimeError("synthetic failure")
    z = indicators.demand_supply_zones(synthetic_daily(symbol))
    z["mtf_bias"] = "mixed"
    z["mtf"] = {"bias": {"1d": indicators._tf_bias(z)}, "alignment": "mixed"}
    z["timeframes"] = {"1d": indicators._compact_tf(z)}
    z["trade_plan"] = indicators.build_trade_plan(z, capital=capital, risk_pct=risk_pct)
    return ok("fake", {**z, "candle_provider": "fake", "bars_used": 320}, symbol=symbol)


def fake_events(symbol: str, days: int = 90, as_of: str | None = None) -> dict[str, Any]:
    stamp = "2026-10-15T10:00:00+05:30"
    ev = [{"event_id": f"{symbol}-res", "symbol": symbol, "exchange": "NSE", "event_type": "results", "announcement_date": "2026-10-15",
           "event_date": "2026-10-15", "event_status": "announced", "event_summary": f"{symbol} Q2 results", "source_url": "https://www.nseindia.com/x",
           "published_at": stamp, "retrieved_at": stamp, "available_at": stamp},
          {"event_id": f"{symbol}-bm", "symbol": symbol, "exchange": "NSE", "event_type": "board_meeting", "announcement_date": "",
           "event_date": "2026-10-28", "event_status": "scheduled", "event_summary": "Board meeting for dividend", "source_url": "https://www.nseindia.com/y",
           "published_at": "", "retrieved_at": stamp, "available_at": stamp}]
    return ok("fake_nse", ev, symbol=symbol)


def synthetic_quarters(symbol: str, n: int = 8) -> list[dict[str, Any]]:
    rng = np.random.default_rng(_seed(symbol) + 7)
    ends = pd.date_range(end="2026-09-30", periods=n, freq="QE")
    rev = 1000 * np.cumprod(1 + rng.normal(0.02, 0.03, n))
    return [{"symbol": symbol, "period_end": e.date().isoformat(), "basis": "consolidated", "source": "fake", "revenue": round(float(r), 1),
             "op_profit": round(float(r) * 0.4, 1), "op_kind": "PPOP~", "pbt": round(float(r) * 0.3, 1), "pat": round(float(r) * 0.22, 1),
             "eps": 5.0, "reported_on": None} for e, r in zip(ends, rev)]


def fake_quarterly(symbol: str, as_of: str | None = None, sub_sector: str | None = None) -> dict[str, Any]:
    from app.store import fundamentals as fq
    rows = synthetic_quarters(symbol)
    return ok("fake", {"series_source": "fake", "basis": "consolidated", "unit": "INR crore", "quarters": rows, "metrics": fq.compute_metrics(rows)},
              symbol=symbol, cache="hit", latest_period=rows[-1]["period_end"], source_errors=[], operating_profit_kind="PPOP (approx.)")


def fake_tools() -> dict[str, Any]:
    flows = ok("fake", [{"category": "FII/FPI", "date": "20-Oct-2026", "net_value_cr": "-800"}, {"category": "DII", "date": "20-Oct-2026", "net_value_cr": "1200"}], scope="market_wide_not_stock_specific")

    def chain(symbol: str, strikecount: int = 10, as_of: str | None = None):
        if symbol not in FNO:
            return unavailable("fake_option_chain", "no option chain: not an F&O stock")
        return ok("fake_option_chain", {"underlying_ltp": 800, "pcr_oi": 1.1, "total_call_oi": 1e6, "total_put_oi": 1.1e6, "day_call_oi_change": 1e4,
                                          "day_put_oi_change": 4e4, "windows": {"1w": "insufficient_history", "1m": "insufficient_history"},
                                          "levels": {"resistance": [], "support": [], "max_pain_approx": 795}})

    def fake_rs(symbol: str, sub_sector: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        return ok("fake", {"signal": "outperforming", "rs_trend": "improving",
                           "rs_vs_sector_pct": {"4w": 2.1, "13w": 3.8, "26w": 5.2},
                           "rs_vs_nifty50_pct": {"4w": 1.5, "13w": 3.0, "26w": 4.1},
                           "stock_returns_pct": {"4w": 3.5, "13w": 7.2, "26w": 9.8},
                           "sector_data_ok": True, "nifty_data_ok": True,
                           "note": "fake RS data"}, symbol=symbol)

    def fake_risk(symbol: str, as_of: str | None = None) -> dict[str, Any]:
        return ok("fake", {"beta_vs_nifty": 1.15, "correlation_nifty": 0.72,
                           "volatility_pct": {"1y": 28.5, "3m": 25.1},
                           "var_95_daily_pct": 2.1, "var_note": "2.1% daily loss at 95% confidence",
                           "max_drawdown_pct": {"3m": -14.3, "6m": -22.1, "1y": -28.5},
                           "risk_level": "medium", "position_size_factor": 0.87,
                           "sizing_note": "Beta 1.15 → multiply standard qty by 0.87",
                           "nifty_data_ok": True, "note": "fake risk data"}, symbol=symbol)

    def fake_pledge(symbol: str) -> dict[str, Any]:
        return ok("fake", {"latest_pledge_pct": 0.0, "trend": "stable", "risk": "low",
                           "quarters": [], "note": "fake pledge data — GoI promoter"}, symbol=symbol)

    return {
        "get_candles": lambda symbol, **k: unavailable("fake", "not used"),
        "get_technical_indicators": fake_indicators,
        "get_demand_supply_zones": fake_zones,
        "get_option_chain": chain,
        "get_relative_strength": fake_rs,
        "get_quant_risk": fake_risk,
        "nse_company_events": fake_events,
        "nse_fii_dii": lambda as_of=None: flows,
        "nse_deals": lambda symbol, days=30, as_of=None: ok("fake", [], symbol=symbol, scope="stock_specific"),
        "nse_shareholding": lambda symbol: ok("fake", [{"date": "30-SEP-2026", "pr_and_prgrp": "62.93", "public_val": "37.07"}], symbol=symbol, scope="stock_specific"),
        "nse_promoter_pledge": fake_pledge,
        "quarterly_fundamentals": fake_quarterly,
        "yf_fundamentals": lambda symbol: ok("fake", {"ratios": {"trailingPE": 12.1, "returnOnEquity": 0.15}, "quarterly": [], "recent_dividends": []}, point_in_time=False),
        "rbi_policy": lambda days=45, as_of=None: ok("fake_rbi", {"policy_rates": {"rbi_repo_rate_percent": None, "needs_verification": True}, "next_mpc_meetings": [], "recent_rbi_releases": []}),
        "foreign_rates": lambda as_of=None: ok("fake_fred", [{"series": "DFF", "latest": 4.1, "change_3m": -0.25, "stale": False}]),
        "macro_snapshot": lambda as_of=None: ok("fake_yf", [{"name": "USDINR", "last": 88.1, "chg_20d_pct": 0.4}]),
        "policy_calendar": lambda days_ahead=60, as_of=None: ok("fake_cal", []),
        "news_search": lambda query, days=7, as_of=None, max_items=12: ok("fake_news", [{"title": "Sector steady", "published_at": "2026-10-19T09:00:00+05:30", "url": "https://x"}]),
    }
