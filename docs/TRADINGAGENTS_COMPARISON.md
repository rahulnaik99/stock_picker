# TradingAgents vs Stock Scoring — Tool & Data Source Comparison

## Summary

Both projects share the same philosophy (local AI agents on Indian NSE equities) but our project uses **more authoritative, more granular data sources**. Where TradingAgents defaults to Yahoo Finance for everything, we prefer NSE XBRL filings, FYERS live candles, and NSE direct APIs.

---

## Side-by-side comparison

| Feature | TradingAgents | Our Project | Winner |
|---|---|---|---|
| **Quarterly results** | `yfinance` `SYMBOL.NS` statements (unofficial, Yahoo-sourced) | **NSE XBRL filings** (authoritative filed numbers) + Yahoo fallback | ✅ Ours |
| **Price / OHLCV chart** | `yfinance` daily bars (delayed) | **FYERS live candles** (1D/1H/15M, live during market hours) + Yahoo fallback | ✅ Ours |
| **Option chain** | Not implemented | **FYERS option chain** → PCR, max pain, call wall, put floor | ✅ Ours |
| **Market movers** | `yfinance` Nifty 500 batch download | `yfinance` batch download (same, our universe ~98 stocks) | = Equal |
| **Company events / NSE announcements** | NSE events API | **NSE events API** (same) | = Equal |
| **FII/DII flows** | NSE FII/DII data | **NSE FII/DII** (same) + stored in SQLite with history | ✅ Ours |
| **Promoter shareholding / pledge** | Not implemented | **NSE shareholding pattern + pledge** (PSU short-circuit) | ✅ Ours |
| **Macro / rates** | Not implemented | **RBI RSS + FRED API** (repo rate, Fed funds, US 10Y, Brent, VIX) | ✅ Ours |
| **News** | Google News RSS | **Google News RSS + IndianAPI** (premium financial news, once-daily shared) | ✅ Ours |
| **Relative strength** | Not implemented | **FYERS stock + Yahoo sector index** → 4w/13w/26w RS | ✅ Ours |
| **Beta / VaR / drawdown** | Not implemented | **FYERS + Nifty 50 date-aligned** → beta (reliable guard), 3m/6m/1y drawdown, VaR 95% | ✅ Ours |
| **Valuation ratios (P/E, P/B, ROE)** | Yahoo snapshot | **Yahoo snapshot** (same, 7-day cache) | = Equal |
| **Health flags** | Rule-based on stored facts | **Rule-based on NSE XBRL quarterly results** | ✅ Ours (better inputs) |
| **Interactive chart** | Static SVG (no interactivity) | **Plotly TradingView-style HTML** (zoom/pan/hover, fundamental overlay) | ✅ Ours |
| **Sector heatmap** | Full Nifty 500 (broad) | Our financial universe (~98 stocks, depth over breadth) | Trade-off |
| **LLM orchestration** | LangGraph | **Plain asyncio** (no langgraph; removed after import bug in v1.2) | ✅ Ours (simpler, works) |

---

## What we borrowed from TradingAgents

| Feature | Where we added it |
|---|---|
| Sidebar nav layout (Market, Stock, Scoring, Data) | `index.html` — full redesign |
| Market overview tab: breadth bar, heatmap, breakouts | `app/market_api.py` `/api/market/movers` + frontend |
| Stock detail tab: KPI cards, candlestick chart, period+EMA toggles | `app/market_api.py` `/api/market/price-history/{symbol}` + frontend SVG chart |
| Financial health section: score, valuation, revenue/PAT/EPS charts | `app/market_api.py` `/api/fundamentals/{sym}/flags` + `/valuation` + `/statements` |
| Annual / Quarterly toggle on financials | Frontend `frSeg` buttons |
| "Score this stock" button linking Market→Scoring | Frontend nav button |

---

## What we chose NOT to adopt

| TradingAgents feature | Reason not adopted |
|---|---|
| **yfinance-only quarterly statements** | Our NSE XBRL store is authoritative (filed numbers, not Yahoo estimates). We expose the same endpoint shape but back it with XBRL. |
| **Screener tab** | We have the full 10-agent scoring pipeline as our screener — more powerful than rule-based ratios. |
| **Paper trading tab** | Out of scope; no order execution intent. |
| **AI Analysis tab (separate)** | Integrated directly into our Scoring tab — same output, no duplication. |
| **Research memory / Q&A** | Not needed; our agent traces + Data Store tab serve the same purpose. |
| **Full Nifty 500 for heatmap** | We focus on the financial sector universe for depth. Market movers can be extended if desired. |
| **Google Calendar sync** | Not needed for our use case. |

---

## Data source reliability hierarchy

```
Level 1 — Filed, authoritative
  NSE XBRL (quarterly PAT, EPS, NII, GNPA, NNPA, CET1)

Level 2 — Live, exchange-direct
  FYERS (OHLCV 1D/1H/15M, option chain OI/PCR, live during market hours)

Level 3 — Official government / central bank
  NSE REST API (events, shareholding, FII/DII, deals, pledge)
  RBI RSS (policy rates, MPC dates)

Level 4 — Third-party, reliable
  FRED API (US Fed funds, US 10Y, ECB, global rates)
  IndianAPI (premium financial news, once-daily)

Level 5 — Unofficial, delayed, best-effort
  Yahoo Finance yfinance (ratios, index prices, batch movers fallback)
  Google News RSS (headlines)
```

TradingAgents operates entirely at Level 5 for financial data. We operate at Levels 1–3 for the data that matters most, falling back to Level 5 only for market-breadth overview (movers, sector heatmap) and ratio snapshots.
