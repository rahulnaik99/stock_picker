"""Relative Strength: how is this stock performing vs its sector index and vs Nifty 50?

RS answers the question the Trader is blind to: even if CANBK is at a demand zone, if it
has been lagging SBIN/PNB/BANKBARODA for 3 months, the demand is weak-quality. If it is
*leading* the sector the demand zone is far more likely to hold.

Stock candles → FYERS (live). Sector/Nifty index → Yahoo Finance (indices; reliable).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.mcp_server.tools.common import clean_symbol, error, ok, unavailable, parse_as_of
from app.mcp_server.tools.market import fetch_candles

# Yahoo Finance tickers for sector indices (free, no token, reliable for indices)
_SECTOR_INDEX_YF = {
    "bank_psu":          "^NSEBANK",
    "bank_private":      "^NSEBANK",
    "nbfc":              "NIFTYFIN.NS",
    "housing_finance":   "NIFTYFIN.NS",
    "insurance_life":    "NIFTYFIN.NS",
    "insurance_non_life": "NIFTYFIN.NS",
    "amc":               "NIFTYFIN.NS",
    "exchange":          "NIFTYFIN.NS",
    "govt_financier":    "NIFTYFIN.NS",
}
_NIFTY50_YF = "^NSEI"
_WINDOWS = {"4w": 20, "13w": 65, "26w": 130}


def _fetch_index(ticker: str, bars: int = 180) -> pd.Series | None:
    """Fetch index daily close from Yahoo Finance (falls back gracefully)."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period=f"{bars + 30}d", auto_adjust=True)
        if df.empty or "Close" not in df.columns:
            return None
        return df["Close"].dropna().tail(bars)
    except Exception:  # noqa: BLE001
        return None


def _ret(series: pd.Series | None, bars: int) -> float | None:
    if series is None or len(series) < bars + 1:
        return None
    curr, prev = float(series.iloc[-1]), float(series.iloc[-1 - bars])
    return round((curr / prev - 1) * 100, 2) if prev > 0 else None


def compute_rs(stock_daily: pd.DataFrame, sub_sector: str | None) -> dict[str, Any]:
    """Pure RS computation — deterministic, testable, no side effects."""
    close = stock_daily["close"]
    sym_rets = {k: _ret(close, v) for k, v in _WINDOWS.items()}

    # Sector index
    idx_key = _SECTOR_INDEX_YF.get(sub_sector or "", _NIFTY50_YF)
    sector = _fetch_index(idx_key)
    nifty  = _fetch_index(_NIFTY50_YF)

    sec_rets   = {k: _ret(sector, v) for k, v in _WINDOWS.items()} if sector is not None else {}
    nifty_rets = {k: _ret(nifty, v)  for k, v in _WINDOWS.items()} if nifty  is not None else {}

    def _diff(sym, idx):
        return round(sym - idx, 2) if sym is not None and idx is not None else None

    rs_vs_sector = {k: _diff(sym_rets.get(k), sec_rets.get(k)) for k in _WINDOWS}
    rs_vs_nifty  = {k: _diff(sym_rets.get(k), nifty_rets.get(k)) for k in _WINDOWS}

    # RS trend: is the 4w RS better or worse than it was 20 bars ago?
    rs_trend = "unknown"
    if sector is not None and len(close) >= 40 and len(sector) >= 40:
        old_sym_4w  = _ret(close.iloc[:-20], 20)
        old_sec_4w  = _ret(sector.iloc[:-20], 20)
        new_rs = rs_vs_sector.get("4w") or 0
        old_rs = _diff(old_sym_4w, old_sec_4w) or 0
        rs_trend = "improving" if new_rs > old_rs + 1.5 else "deteriorating" if new_rs < old_rs - 1.5 else "stable"

    # Composite signal from 13w RS vs sector
    rs13 = rs_vs_sector.get("13w") or 0
    rs4  = rs_vs_sector.get("4w")  or 0
    signal = ("leading"       if rs13 >  5 and rs4 >  1 else
              "outperforming" if rs13 >  2 else
              "lagging"       if rs13 < -5 and rs4 < -1 else
              "underperforming" if rs13 < -2 else "inline")

    return {
        "sector_index_yf":  idx_key,
        "stock_returns_pct":  sym_rets,
        "rs_vs_sector_pct":   rs_vs_sector,   # + = outperforming sector
        "rs_vs_nifty50_pct":  rs_vs_nifty,    # + = outperforming broader market
        "rs_trend":      rs_trend,
        "signal":        signal,
        "sector_data_ok": sector is not None,
        "nifty_data_ok":  nifty  is not None,
        "note": ("RS > 0 = outperforming sector. Leading + improving = highest long conviction. "
                 "Lagging + deteriorating = consider size-down or skip even at strong demand zone."),
    }


def get_relative_strength(symbol: str, sub_sector: str | None = None,
                           as_of: str | None = None) -> dict[str, Any]:
    """Compute relative strength of `symbol` vs its sector index and Nifty 50.
    Uses FYERS daily candles for the stock; Yahoo Finance for sector/Nifty indices (free, no token)."""
    src = "relative_strength"
    sym = clean_symbol(symbol)
    try:
        daily, provider = fetch_candles(sym, "D", 200, as_of)
        if len(daily) < 30:
            return unavailable(src, f"insufficient daily candles for {sym} (got {len(daily)})")
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)
    data = compute_rs(daily, sub_sector)
    data["candle_provider"] = provider
    data["bars_used"] = int(len(daily))
    return ok(src, data, symbol=sym)
