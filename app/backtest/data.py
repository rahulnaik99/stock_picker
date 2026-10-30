"""The only networked part of the backtest: load daily OHLCV history from Yahoo into an in-memory panel.

Yahoo is used here (not FYERS) on purpose: it is free, needs no daily token, and serves many years of adjusted
daily candles in one call - ideal for an offline backtest. Live scoring still uses FYERS; the small differences
between providers' daily closes do not matter for a rank-based edge study.
"""
from __future__ import annotations

from typing import Sequence

import pandas as pd

from app.mcp_server.tools.common import clean_symbol, yahoo_symbol

# A liquid, diversified NSE financials basket for the backtest default (private + PSU banks, NBFCs / financiers,
# insurers, AMC / market-infra) - a real cross-section, not six correlated PSU banks.
DEFAULT_UNIVERSE = [
    "HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK", "INDUSINDBK", "FEDERALBNK", "IDFCFIRSTB",
    "SBIN", "BANKBARODA", "PNB", "CANBK", "UNIONBANK",
    "BAJFINANCE", "BAJAJFINSV", "CHOLAFIN", "SHRIRAMFIN", "MUTHOOTFIN", "SBICARD", "PFC", "RECLTD", "LICHSGFIN",
    "HDFCLIFE", "SBILIFE", "ICICIGI", "ICICIPRULI",
    "HDFCAMC", "BSE", "CDSL",
]


def load_yahoo_daily(symbols: Sequence[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """Return {symbol -> DataFrame[time, open, high, low, close, volume]} for each symbol with usable data."""
    import yfinance as yf

    syms = [clean_symbol(s) for s in symbols]
    panel: dict[str, pd.DataFrame] = {}
    if len(syms) == 1:
        # yf.Ticker.history() always returns a clean flat DataFrame - no MultiIndex surprises
        sym, tk = syms[0], yahoo_symbol(syms[0])
        try:
            raw = yf.Ticker(tk).history(start=start, end=end, auto_adjust=True)
            if raw is not None and not raw.empty:
                df = raw.reset_index().rename(columns={"Date": "time", "Datetime": "time", "Open": "open",
                                                       "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
                df = _normalise(df)
                if df is not None and len(df):
                    panel[sym] = df
        except Exception:  # noqa: BLE001
            pass
        return panel
    # multiple symbols: yf.download with group_by="ticker" returns a MultiIndex DataFrame
    tickers = [yahoo_symbol(s) for s in syms]
    raw = yf.download(tickers, start=start, end=end, interval="1d", auto_adjust=True,
                      group_by="ticker", progress=False, threads=True)
    for sym, tk in zip(syms, tickers):
        try:
            sub = raw[tk]
        except (KeyError, TypeError):
            continue
        df = _normalise(sub)
        if df is not None and len(df):
            panel[sym] = df
    return panel


def _normalise(sub: pd.DataFrame) -> pd.DataFrame | None:
    """Yahoo frame (DatetimeIndex, columns Open/High/Low/Close/Volume) -> the panel schema. None if unusable."""
    if sub is None or sub.empty or "Close" not in sub.columns:
        return None
    df = sub.reset_index().rename(columns={"Date": "time", "Datetime": "time", "Open": "open", "High": "high",
                                           "Low": "low", "Close": "close", "Volume": "volume"})
    cols = ["time", "open", "high", "low", "close", "volume"]
    if not all(c in df.columns for c in cols):
        return None
    df = df[cols].dropna(subset=["close"]).copy()
    df["time"] = pd.to_datetime(df["time"])
    df["volume"] = df["volume"].fillna(0)
    return df.sort_values("time").reset_index(drop=True)


def rebalance_dates(panel: dict[str, pd.DataFrame], freq: str = "W") -> list[pd.Timestamp]:
    """Trading-day rebalance dates at the given pandas frequency (e.g. 'W' weekly, 'ME' month-end, 'D' daily),
    snapped to the nearest preceding actual trading day present in the union of all symbols' candles."""
    all_times = pd.DatetimeIndex(sorted({t for df in panel.values() for t in df["time"]}))
    if all_times.empty:
        return []
    if freq.upper() == "D":
        return list(all_times)
    anchors = pd.date_range(all_times.min(), all_times.max(), freq=freq)
    out: list[pd.Timestamp] = []
    pos = all_times.searchsorted(anchors, side="right") - 1
    for p in pos:
        if p >= 0:
            out.append(all_times[p])
    # dedupe while preserving order
    seen: set[pd.Timestamp] = set()
    return [t for t in out if not (t in seen or seen.add(t))]
