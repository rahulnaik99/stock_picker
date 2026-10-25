"""Yahoo Finance via yfinance: fundamentals, candle fallback, index relative strength.

Free but unofficial: fields can be missing and the schema changes. Never the sole authority;
material facts are cross-checked against NSE filings by the Fundamental agent.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from app.mcp_server.providers.common import IST, ProviderError, Unavailable
from app.mcp_server.providers.symbols import yahoo_symbol

INFO_FIELDS = [
    "longName", "sector", "industry", "marketCap", "currentPrice", "trailingPE", "forwardPE", "priceToBook",
    "bookValue", "returnOnEquity", "returnOnAssets", "profitMargins", "earningsGrowth", "revenueGrowth",
    "dividendYield", "dividendRate", "payoutRatio", "trailingEps", "debtToEquity",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "heldPercentInstitutions", "heldPercentInsiders", "recommendationMean",
]
STATEMENT_ROWS = ["Total Revenue", "Net Interest Income", "Pretax Income", "Net Income", "Basic EPS",
                  "Provision For Credit Losses", "Operating Income"]


def _yf():
    import yfinance as yf  # imported lazily: heavy import
    return yf


def _clean(v: Any) -> Any:
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return float(v) if hasattr(v, "__float__") and not isinstance(v, (str, bool)) else v


def fundamentals(symbol: str) -> dict:
    t = _yf().Ticker(yahoo_symbol(symbol))
    info = t.info or {}
    if not info or not (info.get("longName") or info.get("shortName")):
        raise Unavailable(f"Yahoo has no profile for {yahoo_symbol(symbol)}")
    out: dict[str, Any] = {"profile_and_ratios": {k: _clean(info.get(k)) for k in INFO_FIELDS if info.get(k) is not None}}
    try:
        q = t.quarterly_income_stmt
        if q is not None and not q.empty:
            rows = {}
            for name in STATEMENT_ROWS:
                if name in q.index:
                    rows[name] = {c.strftime("%Y-%m-%d"): _clean(v) for c, v in q.loc[name].head(5).items()}
            out["quarterly_results"] = rows
    except Exception as e:  # noqa: BLE001  (Yahoo statements are flaky; absence is reported, not fatal)
        out["quarterly_results_error"] = str(e)
    try:
        dv = t.dividends
        if dv is not None and len(dv):
            out["recent_dividends"] = [{"date": d.strftime("%Y-%m-%d"), "amount": float(a)} for d, a in dv.tail(4).items()]
    except Exception:  # noqa: BLE001
        pass
    out["name"] = info.get("longName") or info.get("shortName")
    out["note"] = "Yahoo data is unofficial; units/currency INR; cross-check material numbers with NSE filings."
    return out


def _download(ticker: str, period: str, interval: str) -> pd.DataFrame:
    df = _yf().Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        raise Unavailable(f"Yahoo returned no {interval} candles for {ticker}")
    df = df.reset_index()
    tcol = "Datetime" if "Datetime" in df.columns else "Date"
    ts = pd.to_datetime(df[tcol])
    ts = ts.dt.tz_localize(IST) if ts.dt.tz is None else ts.dt.tz_convert(IST)
    if interval == "1d":
        ts = ts.dt.normalize()
    out = pd.DataFrame({"ts": ts, "open": df["Open"], "high": df["High"], "low": df["Low"],
                        "close": df["Close"], "volume": df["Volume"]})
    return out.dropna(subset=["close"]).reset_index(drop=True)


def candles_bundle(symbol: str, as_of: datetime) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    ysym = yahoo_symbol(symbol)
    daily = _download(ysym, "2y", "1d")
    try:
        intraday = _download(ysym, "5d", "15m")
    except Unavailable:
        intraday = None
    return daily, intraday


def relative_strength(symbol: str, index_ticker: str, benchmark: str = "^NSEI") -> dict:
    """Stock vs its sector index vs Nifty 50 over 1m / 3m (completed daily closes)."""
    def rets(tk: str) -> dict[str, float | None]:
        d = _download(tk, "1y", "1d")["close"]
        f = lambda n: round((float(d.iloc[-1]) / float(d.iloc[-1 - n]) - 1) * 100, 2) if len(d) > n else None  # noqa: E731
        return {"1m": f(21), "3m": f(63), "last": round(float(d.iloc[-1]), 2)}
    res = {"stock": rets(yahoo_symbol(symbol))}
    for name, tk in (("sector_index", index_ticker), ("benchmark_nifty50", benchmark)):
        try:
            res[name] = {"ticker": tk, **rets(tk)}
        except (Unavailable, ProviderError, KeyError, IndexError) as e:
            res[name] = {"ticker": tk, "unavailable": str(e)}
    return res


def last_close(ticker: str) -> dict:
    d = _download(ticker, "1mo", "1d")
    return {"ticker": ticker, "last": round(float(d["close"].iloc[-1]), 4), "date": d["ts"].iloc[-1].date().isoformat(),
            "chg_1m_pct": round((float(d["close"].iloc[-1]) / float(d["close"].iloc[0]) - 1) * 100, 2)}
