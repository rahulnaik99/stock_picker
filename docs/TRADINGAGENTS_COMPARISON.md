# TradingAgents vs Stock Scoring — Tool & Data Source Comparison

## Summary

Both projects share the same philosophy (local AI agents on Indian NSE equities) but our project uses **more authoritative, more granular data sources**. Where TradingAgents defaults to Yahoo Finance for everything, we prefer NSE XBRL filings, FYERS live candles, and NSE direct APIs.

---

## Side-by-side comparison

| Feature | TradingAgents | Our Project | Winner |
|---|---|---|---|
| **Quarterly results** | `yfinance` SYMBOL.NS statements (unofficial, Yahoo-sourced) | **NSE XBRL filings** (authoritative filed numbers, split-adjusted) + Yahoo fallback | ✅ Ours |
| **Price / OHLCV chart** | `yfinance` daily bars (delayed) | **FYERS live candles** (1D/1H/15M) + Yahoo fallback | ✅ Ours |
| **Option chain** | Not implemented | **FYERS option chain** → PCR, max pain, call wall, put floor, OI quadrant | ✅ Ours |
| **Market movers** | `yfinance` Nifty 500 batch | `yfinance` batch — **232 Nifty symbols** with universe switcher + superset cache | ✅ Ours (more universes) |
| **OI Activity** | Not implemented | Stored `oi_snapshots` → Long Buildup / Short Covering / Short Buildup / Long Unwinding | ✅ Ours |
| **Company events** | NSE events API | **NSE events API** (same) | = Equal |
| **FII/DII flows** | NSE FII/DII data | **NSE FII/DII** (same) + stored in SQLite with history | ✅ Ours |
| **Promoter shareholding / pledge** | Not implemented | **NSE shareholding + pledge** (PSU banks short-circuit — GoI never pledges) | ✅ Ours |
| **Macro / rates** | Not implemented | **RBI RSS + FRED API** (repo, Fed funds, US 10Y, Brent, VIX) | ✅ Ours |
| **News** | Google News RSS | **Google News RSS + IndianAPI** (premium financial news, once-daily shared) | ✅ Ours |
| **Relative strength** | Not implemented | **FYERS stock + Yahoo sector index** → 4w/13w/26w RS vs Nifty Bank | ✅ Ours |
| **Beta / VaR / drawdown** | Not implemented | **FYERS + Nifty 50 date-aligned** → beta (guarded), drawdown 3m/6m/1y, VaR 95% | ✅ Ours |
| **Valuation ratios** | Yahoo snapshot | **Yahoo snapshot** (same, 7-day cache) | = Equal |
| **Financial health flags** | Rule-based on Yahoo facts | **Rule-based on NSE XBRL** (authoritative inputs) | ✅ Ours |
| **Interactive chart** | Static SVG | **Plotly TradingView-style HTML** (zoom/pan/hover, fundamental overlay, 15-min zoom) | ✅ Ours |
| **Sector heatmap** | Full Nifty 500 | Our financial universe (depth) + Nifty indices (breadth) | ✅ Ours (both) |
| **EPS split handling** | Raw Yahoo — no adjustment | **Automatic split detection** in `horizon_metrics` — pre-split EPS rescaled to current face value | ✅ Ours |
| **LLM orchestration** | LangGraph | **Plain asyncio** (LangGraph removed after v1.2 import bug) | ✅ Ours (simpler) |

---

## What we borrowed from TradingAgents

| Feature | Where |
|---|---|
| Sidebar nav layout (Browse / Analysis / Storage) | `app/static/index.html` — full redesign |
| Market overview tab: breadth bar, heatmap, breakouts | `app/market_api.py` + frontend |
| Stock detail tab: KPI cards, SVG candlestick chart, period/EMA toggles | `app/market_api.py` + frontend |
| Financial health section: score 0–100, valuation grid, bar charts | `app/market_api.py` endpoints + SVG charts |
| Annual / Quarterly toggle on financials | Frontend `frSeg` buttons |
| "Score this stock" cross-link from Market → Scoring | Frontend nav button |

---

## What we added on top of TradingAgents

| Feature | Details |
|---|---|
| **Universe switcher** | Financial / Nifty 500 / 50 / Bank / IT / Small / Mid / Large Cap |
| **Superset cache** | Nifty 500 downloaded once; sub-index views filter from the same payload (0 re-downloads) |
| **OI Activity tab** | Long Buildup / Short Covering / Short Buildup / Long Unwinding from stored `oi_snapshots` |
| **Loading overlay** | Blurred spinner during data fetch; freshness ticker shows "Xm ago"; LIVE/CACHED badge |
| **Duplicate-free gainers/losers** | Strict top-6/bottom-6 with dedup — a symbol can only appear once |
| **Watchlists** | Named persistent stock lists, run any list directly |
| **Data Store tab** | Browse + prune every SQLite table from the UI |
| **Retry-once-then-skip** | Each agent retries once; required agents failing twice skips the stock |

---

## What we chose NOT to adopt

| TradingAgents feature | Reason |
|---|---|
| yfinance-only quarterly statements | Our NSE XBRL store is authoritative; we expose the same endpoint shape but backed by filed data |
| Screener tab | Our 10-agent pipeline is the screener — more powerful than ratio rules |
| Paper trading tab | Out of scope; no order execution |
| Separate AI Analysis tab | Integrated into Scoring tab — same output, no duplication |
| Research memory / Q&A | Agent traces + Data Store tab cover this |
| Google Calendar sync | Not needed |

---

## Data source reliability hierarchy

```
Level 1 — Filed, authoritative
  NSE XBRL (quarterly PAT, EPS, NII, GNPA, NNPA, CET1) — split-adjusted in horizon_metrics

Level 2 — Live, exchange-direct
  FYERS (OHLCV 1D/1H/15M, option chain OI/PCR, live during market hours)

Level 3 — Official government / central bank
  NSE REST API (events, shareholding, FII/DII, deals, pledge)
      ↳ Note: NSE returns 403 for unauthenticated GETs — tools work via session init
  RBI RSS (policy rates, MPC dates)

Level 4 — Third-party, reliable
  FRED API (US Fed funds, US 10Y, ECB, global rates)
  IndianAPI (premium financial news, once-daily market-wide)

Level 5 — Unofficial, delayed, best-effort
  Yahoo Finance yfinance (ratios snapshot, index prices, Nifty universe batch)
      ↳ Note: direct chart endpoint returns 429 (rate-limited) — yfinance library itself is unaffected
  Google News RSS (headlines)
```

TradingAgents operates entirely at Level 5 for financial data. We use Levels 1–3 for the data that matters most, falling back to Level 5 only for market-breadth overview and ratio snapshots.
