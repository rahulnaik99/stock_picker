"""Market overview + Stock detail API (ported from the TradingAgents UI).

These endpoints power the two new browse tabs:
- Market tab  → /api/market/movers         (Nifty-500 daily movers, heatmap, breakouts)
- Stock tab   → /api/market/price-history   (OHLCV for the chart — FYERS first, Yahoo fallback)
               /api/fundamentals/{sym}/statements  (quarterly/annual — NSE XBRL, our source)
               /api/fundamentals/{sym}/flags       (rule-based health flags)
               /api/fundamentals/{sym}/valuation   (Yahoo P/E · P/B · ROE snapshot)

Design note on data sources (how this differs from TradingAgents):
- TradingAgents pulls EVERYTHING from yfinance (SYMBOL.NS). We keep that only for the Nifty-500
  movers batch (no better bulk source) and as a chart fallback.
- For the single-stock chart we prefer FYERS candles (live, our token) and only fall back to Yahoo.
- For statements we use our NSE XBRL filings store (authoritative filed numbers) instead of Yahoo.
"""
from __future__ import annotations

import time
import threading
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_MOVERS_CACHE: dict[str, Any] = {}
_MOVERS_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
#  STOCK TAB — price history (FYERS first, Yahoo fallback)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/api/market/price-history/{symbol}", tags=["market"])
def price_history(symbol: str, period: str = Query(default="6mo", pattern="^(1mo|3mo|6mo|1y)$")) -> dict[str, Any]:
    """Daily OHLCV for the Stock-tab chart. Prefers FYERS; falls back to Yahoo."""
    sym = "".join(c for c in symbol.upper() if c.isalnum())
    if not sym:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    days = {"1mo": 32, "3mo": 95, "6mo": 190, "1y": 380}[period]
    rows: list[dict[str, Any]] = []
    source = ""
    # 1) FYERS (our live source)
    try:
        from app.mcp_server.tools.market import fetch_candles
        import pandas as pd
        df, provider = fetch_candles(sym, "D", days, None)
        if df is not None and len(df):
            for _, r in df.iterrows():
                rows.append({"date": str(pd.to_datetime(r["time"]).date()),
                             "open": float(r["open"]), "high": float(r["high"]),
                             "low": float(r["low"]), "close": float(r["close"]),
                             "volume": int(r["volume"] or 0)})
            source = f"{provider} daily candles"
    except Exception:  # noqa: BLE001
        pass
    # 2) Yahoo fallback
    if not rows:
        try:
            import yfinance as yf, math
            frame = yf.download(tickers=f"{sym}.NS", period=period, interval="1d", auto_adjust=False,
                                progress=False, threads=False, timeout=15)
            if frame is not None and not frame.empty:
                if getattr(frame.columns, "nlevels", 1) > 1:
                    frame.columns = frame.columns.get_level_values(0)
                for stamp, r in frame.iterrows():
                    try:
                        o, h, l, c, v = (float(r[k]) for k in ("Open", "High", "Low", "Close", "Volume"))
                        if not all(math.isfinite(x) for x in (o, h, l, c)) or c <= 0:
                            continue
                        rows.append({"date": stamp.date().isoformat() if hasattr(stamp, "date") else str(stamp)[:10],
                                     "open": o, "high": h, "low": l, "close": c, "volume": max(0, int(v or 0))})
                    except Exception:  # noqa: BLE001
                        continue
                source = "Yahoo Finance daily bars; delayed/coverage-dependent"
        except Exception:  # noqa: BLE001
            pass
    if not rows:
        raise HTTPException(status_code=502, detail=f"Price history unavailable for {sym}")
    latest = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    change = ((latest["close"] / prev["close"]) - 1) * 100 if prev and prev["close"] else None
    hi = max(r["high"] for r in rows)
    lo = min(r["low"] for r in rows)
    return {"symbol": sym, "period": period, "rows": rows, "latest": latest,
            "previous_close": prev["close"] if prev else None, "change_pct": change,
            "period_high": hi, "period_low": lo, "source": source}


# ─────────────────────────────────────────────────────────────────────────────
#  STOCK TAB — fundamentals statements (our NSE XBRL store)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/api/fundamentals/{symbol}/statements", tags=["market"])
def statements(symbol: str, period_type: str = Query(default="quarterly", pattern="^(annual|quarterly)$")) -> dict[str, Any]:
    """Quarterly (or FY-aggregated annual) results from the NSE-XBRL-backed store."""
    sym = "".join(c for c in symbol.upper() if c.isalnum())
    from app.mcp_server.tools.fundamentals import quarterly_fundamentals
    res = quarterly_fundamentals(sym)
    if res.get("status") != "ok":
        return {"symbol": sym, "period_type": period_type, "periods": [], "note": res.get("message", "no data")}
    data = res["data"]
    quarters = data.get("quarters", [])
    if period_type == "annual":
        # aggregate quarters into fiscal years (Apr–Mar)
        from datetime import date
        fy: dict[int, list[dict]] = {}
        for q in quarters:
            d = date.fromisoformat(q["period_end"])
            fy.setdefault(d.year if d.month <= 3 else d.year + 1, []).append(q)
        periods = []
        for y in sorted(fy):
            qs = fy[y]
            if len(qs) < 4:
                continue
            agg = {"period_end": f"FY{y}", "label": f"FY{str(y)[2:]}"}
            for k in ("revenue", "op_profit", "pat"):
                vals = [q.get(k) for q in qs if q.get(k) is not None]
                agg[k] = round(sum(vals), 2) if vals else None
            eps_vals = [q.get("eps") for q in qs if q.get("eps") is not None]
            agg["eps"] = round(sum(eps_vals), 2) if eps_vals else None
            periods.append(agg)
        periods = periods[-5:]
    else:
        periods = [{"period_end": q["period_end"], "label": q["period_end"][:7],
                    "revenue": q.get("revenue"), "op_profit": q.get("op_profit"),
                    "pat": q.get("pat"), "eps": q.get("eps")} for q in quarters[-8:]]
    return {"symbol": sym, "period_type": period_type, "unit": data.get("unit"),
            "basis": data.get("basis"), "series_source": data.get("series_source"),
            "latest_filing": data.get("latest_filing"), "periods": periods,
            "metrics": data.get("metrics")}


# ─────────────────────────────────────────────────────────────────────────────
#  STOCK TAB — valuation snapshot (Yahoo ratios) + rule-based health flags
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/api/fundamentals/{symbol}/valuation", tags=["market"])
def valuation(symbol: str) -> dict[str, Any]:
    sym = "".join(c for c in symbol.upper() if c.isalnum())
    from app.mcp_server.tools.fundamentals import yf_fundamentals
    res = yf_fundamentals(sym)
    ratios = (res.get("data") or {}).get("ratios", {}) if res.get("status") == "ok" else {}
    return {"symbol": sym,
            "trailing_pe": ratios.get("trailingPE"),
            "forward_pe": ratios.get("forwardPE"),
            "price_to_book": ratios.get("priceToBook"),
            "roe": ratios.get("returnOnEquity"),
            "dividend_yield": ratios.get("dividendYield"),
            "market_cap": ratios.get("marketCap"),
            "note": res.get("note")}


@router.get("/api/fundamentals/{symbol}/flags", tags=["market"])
def flags(symbol: str) -> dict[str, Any]:
    """Rule-based financial-health flags + a 0-100 score (ported from TradingAgents' financial_flags)."""
    sym = "".join(c for c in symbol.upper() if c.isalnum())
    from app.mcp_server.tools.fundamentals import quarterly_fundamentals
    res = quarterly_fundamentals(sym)
    out_flags: list[dict[str, str]] = []
    score = 50
    if res.get("status") == "ok":
        qs = res["data"].get("quarters", [])
        pats = [q.get("pat") for q in qs if q.get("pat") is not None]
        revs = [q.get("revenue") for q in qs if q.get("revenue") is not None]
        # Net income increasing QoQ
        if len(pats) >= 2 and pats[-1] > pats[-2]:
            out_flags.append({"kind": "good", "text": "Net income increased quarter over quarter"}); score += 10
        elif len(pats) >= 2 and pats[-1] < pats[-2]:
            out_flags.append({"kind": "bad", "text": "Net income fell quarter over quarter"}); score -= 10
        # Revenue falling streak
        falling = 0
        for a, b in zip(revs, revs[1:]):
            falling = falling + 1 if b < a else 0
        if falling >= 3:
            out_flags.append({"kind": "bad", "text": f"Revenue fell {falling} quarters in a row"}); score -= 15
        # Net margin expanding
        if len(qs) >= 2:
            m_new = (qs[-1].get("pat") or 0) / (qs[-1].get("revenue") or 1)
            m_old = (qs[-2].get("pat") or 0) / (qs[-2].get("revenue") or 1)
            if m_new > m_old:
                out_flags.append({"kind": "good", "text": "Net margin is expanding"}); score += 10
        # YoY net income
        if len(pats) >= 5 and pats[-1] > pats[-5]:
            out_flags.append({"kind": "good", "text": "Net income increased year over year"}); score += 10
    score = max(0, min(100, score))
    return {"symbol": sym, "score": score, "flags": out_flags,
            "note": "Rule-based flags on NSE-XBRL quarterly results. Not advice."}


# ─────────────────────────────────────────────────────────────────────────────
#  MARKET TAB — Nifty-500 movers (yfinance batch — no better bulk source)
# ─────────────────────────────────────────────────────────────────────────────
def _members() -> list[dict[str, Any]]:
    """Universe members for the movers scan. Uses our universe.csv (financials) by default."""
    try:
        from app.service.settings import get_settings
        import csv
        path = get_settings().path("app/config/universe.csv")
        with open(path, newline="") as fh:
            return [{"symbol": r["symbol"].strip().upper(),
                     "company_name": r.get("company_name", ""),
                     "sector": r.get("sub_sector", "")} for r in csv.DictReader(fh) if r.get("symbol")]
    except Exception:  # noqa: BLE001
        return []


@router.get("/api/market/movers", tags=["market"])
def movers(refresh: bool = Query(default=False)) -> dict[str, Any]:
    """Daily movers across the configured universe from Yahoo daily bars (5-min cached)."""
    now = time.time()
    with _MOVERS_LOCK:
        cached = _MOVERS_CACHE.get("payload")
        if cached and not refresh and now - float(_MOVERS_CACHE.get("at", 0)) < 300:
            return {**cached, "cached": True}
    members = _members()
    if not members:
        raise HTTPException(status_code=503, detail="Universe snapshot unavailable")
    try:
        import yfinance as yf, pandas as pd
        tickers = [f"{m['symbol']}.NS" for m in members]
        frame = yf.download(tickers=tickers, period="1mo", interval="1d", group_by="ticker",
                            auto_adjust=False, progress=False, threads=True, timeout=25)
        if frame is None or frame.empty:
            raise ValueError("provider returned no daily bars")
        rows = []
        for m in members:
            sym = m["symbol"]; tk = f"{sym}.NS"
            try:
                part = frame[tk] if isinstance(frame.columns, pd.MultiIndex) else frame
                part = part.dropna(subset=["Close", "High", "Low"]).tail(22)
                if part.empty:
                    continue
                closes = [float(v) for v in part["Close"].tolist()]
                highs = [float(v) for v in part["High"].tolist()]
                lows = [float(v) for v in part["Low"].tolist()]
                vols = [max(0, int(v or 0)) for v in part["Volume"].tolist()]
                close = closes[-1]; prev = closes[-2] if len(closes) > 1 else None
                change = ((close / prev) - 1) * 100 if prev else 0.0

                def ret(n):
                    return (closes[-1] / closes[-n - 1] - 1) * 100 if len(closes) > n and closes[-n - 1] else None
                down = 0
                for i in range(len(closes) - 1, 0, -1):
                    if closes[i] < closes[i - 1]: down += 1
                    else: break
                prior = vols[-6:-1]
                avgv = sum(prior) / len(prior) if prior else None
                rows.append({"symbol": sym, "company_name": m["company_name"], "sector": m["sector"],
                             "close": close, "change_pct": change, "return_3d_pct": ret(3),
                             "consecutive_down_days": down,
                             "volume_ratio_5d": (vols[-1] / avgv) if avgv and avgv > 0 else None,
                             "volume": vols[-1], "traded_value": close * vols[-1]})
            except Exception:  # noqa: BLE001
                continue
        if not rows:
            raise ValueError("no usable ticker bars")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Market data unavailable: {type(exc).__name__}: {str(exc)[:160]}") from exc
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    payload = {
        "universe": "Configured universe", "source": "Yahoo Finance daily bars; delayed/coverage-dependent",
        "rows": rows, "gainers": rows[:12], "losers": sorted(rows, key=lambda r: r["change_pct"])[:12],
        "highest_volume": sorted(rows, key=lambda r: r["volume"], reverse=True)[:12],
        "consistent_losers": sorted([r for r in rows if r.get("consecutive_down_days", 0) >= 2],
                                    key=lambda r: (r["consecutive_down_days"], -(r.get("return_3d_pct") or 0)), reverse=True)[:25],
        "three_day_winners": sorted([r for r in rows if (r.get("return_3d_pct") or 0) > 0],
                                    key=lambda r: r["return_3d_pct"], reverse=True)[:25],
        "high_volume_3d": sorted([r for r in rows if r.get("volume_ratio_5d") is not None],
                                 key=lambda r: r["volume_ratio_5d"], reverse=True)[:25],
        "breadth": {"advancers": sum(r["change_pct"] > 0 for r in rows),
                    "decliners": sum(r["change_pct"] < 0 for r in rows),
                    "unchanged": sum(r["change_pct"] == 0 for r in rows)},
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with _MOVERS_LOCK:
        _MOVERS_CACHE.update(at=time.time(), payload=payload)
    return {**payload, "cached": False}
