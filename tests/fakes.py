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
                                          "day_put_oi_change": 4e4, "windows": {"1w": "insufficient_history", "1m": "insufficient_history"}})

    return {
        "get_candles": lambda symbol, **k: unavailable("fake", "not used"),
        "get_technical_indicators": fake_indicators,
        "get_option_chain": chain,
        "nse_company_events": fake_events,
        "nse_fii_dii": lambda as_of=None: flows,
        "nse_deals": lambda symbol, days=30, as_of=None: ok("fake", [], symbol=symbol, scope="stock_specific"),
        "nse_shareholding": lambda symbol: unavailable("fake", "no shareholding in fixture"),
        "quarterly_fundamentals": fake_quarterly,
        "yf_fundamentals": lambda symbol: ok("fake", {"ratios": {"trailingPE": 12.1, "returnOnEquity": 0.15}, "quarterly": [], "recent_dividends": []}, point_in_time=False),
        "rbi_policy": lambda days=45, as_of=None: ok("fake_rbi", {"policy_rates": {"rbi_repo_rate_percent": None, "needs_verification": True}, "next_mpc_meetings": [], "recent_rbi_releases": []}),
        "foreign_rates": lambda as_of=None: ok("fake_fred", [{"series": "DFF", "latest": 4.1, "change_3m": -0.25, "stale": False}]),
        "macro_snapshot": lambda as_of=None: ok("fake_yf", [{"name": "USDINR", "last": 88.1, "chg_20d_pct": 0.4}]),
        "policy_calendar": lambda days_ahead=60, as_of=None: ok("fake_cal", []),
        "news_search": lambda query, days=7, as_of=None, max_items=12: ok("fake_news", [{"title": "Sector steady", "published_at": "2026-10-19T09:00:00+05:30", "url": "https://x"}]),
    }
