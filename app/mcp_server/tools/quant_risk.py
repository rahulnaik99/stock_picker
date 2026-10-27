"""Quantitative risk metrics: beta, drawdown profile, Value at Risk, volatility, correlation.

Answers: "how risky is this stock and how should I size my position?"
The Trader currently sizes qty = risk_budget / per_share_risk without knowing the stock's
market sensitivity. A β = 1.8 stock needs different sizing than β = 0.6.

Stock candles → FYERS. Nifty 50 → Yahoo Finance.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.mcp_server.tools.common import clean_symbol, error, ok, unavailable, parse_as_of
from app.mcp_server.tools.market import fetch_candles

_NIFTY50_YF = "^NSEI"


def _fetch_nifty(bars: int = 400) -> pd.Series | None:
    try:
        import yfinance as yf
        df = yf.Ticker(_NIFTY50_YF).history(period=f"{bars + 30}d", auto_adjust=True)
        return df["Close"].dropna().tail(bars) if not df.empty and "Close" in df.columns else None
    except Exception:  # noqa: BLE001
        return None


def compute_quant_risk(daily: pd.DataFrame, nifty: pd.Series | None) -> dict[str, Any]:
    """Pure risk computation — deterministic, testable, no side effects."""
    close = daily["close"].values.astype(float)
    n = len(close)

    # ── returns ──
    rets = np.diff(np.log(close))   # log daily returns

    # ── volatility ──
    vol_1y = round(float(np.std(rets[-252:]) * np.sqrt(252) * 100), 2) if n >= 252 else None
    vol_3m = round(float(np.std(rets[-63:])  * np.sqrt(252) * 100), 2) if n >= 63  else None

    # ── Value at Risk (historical, 95%) ──
    var_95 = round(abs(float(np.percentile(rets[-252:], 5))) * 100, 3) if n >= 252 else None
    var_txt = f"{var_95}% daily loss at 95% confidence" if var_95 else None

    # ── max drawdown ──
    def _mdd(c):
        if len(c) < 2: return None
        p = np.maximum.accumulate(c)
        dd = (c - p) / p
        return round(float(dd.min()) * 100, 2)

    mdd = {"3m": _mdd(close[-63:]) if n >= 63 else None,
           "6m": _mdd(close[-126:]) if n >= 126 else None,
           "1y": _mdd(close[-252:]) if n >= 252 else None}

    # ── beta + correlation vs Nifty ──
    beta = corr = None
    if nifty is not None and len(nifty) > 30:
        nf = nifty.values.astype(float)
        min_len = min(len(rets), len(nf) - 1)
        nf_rets = np.diff(np.log(nf))[-min_len:]
        st_rets  = rets[-min_len:]
        if len(st_rets) > 20:
            cov = np.cov(st_rets, nf_rets)
            var_nf = np.var(nf_rets)
            beta = round(float(cov[0, 1] / var_nf), 2) if var_nf > 0 else None
            corr = round(float(np.corrcoef(st_rets, nf_rets)[0, 1]), 3)

    # ── risk category ──
    risk_level = "high"
    if beta is not None and vol_1y is not None:
        if beta < 0.8 and vol_1y < 25:
            risk_level = "low"
        elif beta < 1.2 and vol_1y < 35:
            risk_level = "medium"
    elif vol_1y is not None:
        risk_level = "low" if vol_1y < 25 else "medium" if vol_1y < 35 else "high"

    # ── position sizing suggestion ──
    size_factor = 1.0
    if beta is not None:
        size_factor = round(1.0 / max(beta, 0.5), 2)  # high beta → reduce size to normalise market exposure
    sizing_note = (f"Beta {beta} → multiply standard qty by {size_factor} to normalise market risk"
                   if beta else "beta unavailable; use standard sizing")

    return {
        "beta_vs_nifty":    beta,
        "correlation_nifty": corr,
        "volatility_pct":   {"1y": vol_1y, "3m": vol_3m},
        "var_95_daily_pct": var_95,
        "var_note":         var_txt,
        "max_drawdown_pct": mdd,
        "risk_level":       risk_level,
        "position_size_factor": size_factor,
        "sizing_note":      sizing_note,
        "nifty_data_ok":    nifty is not None,
        "note": ("Beta > 1 = amplified market moves. High VaR + deep drawdowns = reduce position size. "
                 "Use position_size_factor to normalise: standard qty × factor = beta-adjusted qty."),
    }


def get_quant_risk(symbol: str, as_of: str | None = None) -> dict[str, Any]:
    """Compute beta, max drawdown (3m/6m/1y), 95% VaR, annualised volatility, and Nifty correlation.
    Returns a `position_size_factor` the Trader can apply to normalise market-beta exposure."""
    src = "quant_risk"
    sym = clean_symbol(symbol)
    try:
        daily, provider = fetch_candles(sym, "D", 400, as_of)
        if len(daily) < 60:
            return unavailable(src, f"insufficient candles for {sym}")
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)
    nifty = _fetch_nifty()
    data  = compute_quant_risk(daily, nifty)
    data["candle_provider"] = provider
    data["bars_used"] = int(len(daily))
    return ok(src, data, symbol=sym)
