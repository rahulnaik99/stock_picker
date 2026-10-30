# Stock Scoring Pipeline

A local, offline-first AI-powered research tool for Indian financial stocks (banks, NBFCs, insurance, AMCs). Runs entirely on your machine — no cloud LLM API required.

> **Research only. No orders are placed.**

---

## What it does

- Pulls live data from NSE, FYERS, Yahoo Finance, RBI, FRED, IndianAPI, and Google News
- Runs **10 specialist AI agents** concurrently per stock (fundamental, industry, 8 technical specialists)
- A **Decision Maker** reconciles all scores → `POTENTIAL_ENTRY`, `WAIT_FOR_SETUP`, or `AVOID`
- **Market tab** — daily movers, heatmap, OI activity, breakouts across Financial/Nifty universes
- **Stock tab** — FYERS-sourced candlestick chart, NSE XBRL financials, health score, valuation
- **Scoring tab** — live agent matrix, ranked results, trace viewer, watchlists
- **Data Store tab** — browse every SQLite table, prune old data

---

## Quick Start

### Prerequisites

| | Notes |
|---|---|
| Python 3.11+ | Tested on macOS M1 |
| [Ollama](https://ollama.ai) | Local LLM server |
| FYERS account | Live candles + option chain |

### Install

```bash
git clone https://github.com/rahulnaik99/stock_picker.git -b dev
cd stock_picker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Pull the default model

```bash
ollama pull nemotron-bolt-e4b-16k
# Optional — for deep-dive mixed config:
ollama pull qwen3:30b
ollama pull nemotron-nano-4b-16k
```

### Configure

```bash
cp .env.example .env
# Minimum required:
FYERS_APP_ID=...
FYERS_ACCESS_TOKEN=...   # generate: python -m scripts.fyers_login
# Optional but recommended:
INDIANAPI_KEY=sk-live-...   # premium financial news
FRED_API_KEY=...            # US macro rates
```

### Start

```bash
uvicorn app.api:app --reload --port 8000
# open http://localhost:8000
```

---

## UI — Four tabs

```
┌──────────────┬──────────────────────────────────────────────────────┐
│ ↗ Stock      │ [topbar: symbol search · API · FYERS · ⭐ Lists]     │
│   Scoring    ├──────────────────────────────────────────────────────┤
│              │  📊 Market   movers · heatmap · OI activity          │
│  ── Browse ──│  📈 Stock    chart · fundamentals · health           │
│  📊 Market   │  ⚙  Scoring  run builder · agent matrix · results   │
│  📈 Stock    │  🗄  Data     browse SQLite tables · prune           │
│  ── Analysis─│                                                      │
│  ⚙  Scoring  │  [⭐ Watchlists — slides from right]                │
│  ── Storage ─│  [🔌 Tool Health — slides from bottom]              │
│  🗄 Data     │                                                      │
└──────────────┴──────────────────────────────────────────────────────┘
```

---

## Market Tab — Universe Switcher

| Option | Coverage |
|---|---|
| 🏦 Financial stocks | 98 NSE financials (our universe) |
| 📊 Nifty 500 | 232 symbols across all Nifty indices |
| Nifty 50 / Bank / IT / Small / Mid / Large Cap | Filtered instantly from Nifty 500 cache |

**Nifty 500 is the superset.** Load it once — all sub-index views filter from the same cached batch (no re-download). Sub-index switching is instant.

### OI Activity (Volume card → OI Activity tab)

Reads stored `oi_snapshots`, compares latest two snapshots per symbol:

| Badge | Signal |
|---|---|
| **Long Buildup** | Price ↑ + OI ↑ |
| **Short Covering** | Price ↑ + OI ↓ |
| **Short Buildup** | Price ↓ + OI ↑ |
| **Long Unwinding** | Price ↓ + OI ↓ |

---

## Scoring — Run Modes

| Mode | Models | Best for |
|---|---|---|
| ⚡ **Bulk scan** | `nemotron-bolt-e4b-16k` everywhere | Daily scan of all 98 stocks (~15 min) |
| 🔬 **Deep dive** | qwen3 + bolt + nano (mixed) | 1–5 shortlisted stocks before trading |

Switch with the radio buttons above the Run button. Deep Dive uses `model_config.mixed.json` for the run duration only — no restart needed.

### Retry-once-then-skip

- Every agent retries once on failure (2 s wait between attempts)
- **Required agents** (fundamental, industry, indicator): both fail → stock skipped, others continue
- **Optional agents**: both fail → error AgentResult, scoring continues with partial data

---

## Bug fixes applied (session history)

| Bug | Fix |
|---|---|
| `quant_risk` beta = −0.15 for PSU bank | Date-aligned inner join on shared calendar dates; unreliable-beta guard (`\|corr\| ≥ 0.2`); `max_adjust=1.0` cap |
| `nse_promoter_pledge` 404 every run | PSU banks short-circuit (GoI never pledges); dual endpoint fallback |
| EPS split artifact (FY2024 EPS 84 → FY2025 EPS 19) | Split detection in `horizon_metrics`; pre-split quarters rescaled to current face value |
| Browser swallowing ↻ Refresh silently | `Cache-Control: no-store` on all movers responses + `_t=<timestamp>` cache-busting parameter on force-refresh |
| Duplicate entries in gainers/losers | Strict deduplication — symbol appears in exactly one column |
| Tool health showing Yahoo 429 / NSE 403 as red | 429 = yellow (rate-limited, working); 403 = yellow (needs session, working) |

---

## Watchlists

Click **⭐ Lists** (top-right). Create named lists (e.g. "PSU Banks", "My Longs"), add your current selection with `+ sel`, run an entire list with one click. Stored in SQLite — survives restarts.

---

## Tool Health

Click **🔌 Tool Health** in the sidebar footer. 8 parallel probes check: Ollama, FYERS, NSE, Yahoo Finance, RBI RSS, FRED, IndianAPI, MLX/OLMX. 🟢 green = OK · 🟡 yellow = rate-limited or key not set but functionally working · 🔴 red = unreachable.

---

## Interactive Chart

`/api/chart/{symbol}/interactive` — Plotly TradingView-style HTML (self-contained, opens in browser):
- Daily candlesticks + EMA overlays + demand/supply zones + event markers
- Quarterly PAT bars with YoY% badges
- Bank KPI badge (GNPA/NNPA/NII/CET1)
- 15-min panel zoomed to last 5 days (not 365 days squished)

The 📊 icon in the results table opens this directly.

---

## Configuration files

| File | Purpose |
|---|---|
| `app/service/models/model_config.json` | LLM assignment per agent (bulk — default) |
| `app/service/models/model_config.mixed.json` | Mixed config for deep-dive |
| `app/config/weights.json` | Agent score weights (must sum to 1.0) |
| `app/config/llm_catalog.json` | Models shown in UI picker |
| `app/config/universe.csv` | Your stock universe |

---

## Data storage — what's saved and when pruned

| Table | Grows | Never auto-deleted |
|---|---|---|
| `fundamentals_q` | Each new quarter per stock | Yes — safe, small |
| `news` | ~10-20 rows/day per query | ⚠ Prune at 90 days |
| `company_events` | ~50-100 events/stock | ⚠ Prune at 180 days |
| `fii_dii_daily` | 2 rows/trading day | Low risk |
| `oi_snapshots` | 1 row/stock/run | ⚠ Prune at 60 days |
| `run_agent_io` | 10 agents × N stocks per run | ⚠ **~57 MB/run** — keep last 100 |
| `watchlists` | Until user deletes | Negligible |

Use the **🗑 Prune** button in the Data Store tab to clean up. The `run_agent_io` table grows fastest — prune regularly.

---

## Project structure

```
stock_scoring/
├── app/
│   ├── api.py                      FastAPI application + all routes
│   ├── market_api.py               Market tab + Stock tab (FastAPI router)
│   ├── Agents/
│   │   ├── Fundamental/            NSE XBRL + news + events analyst
│   │   ├── Industry/               RBI + FRED + Nifty macro analyst
│   │   └── Technical/
│   │       ├── Indicator/          EMA/RSI/MACD/ADX/zones (Python)
│   │       ├── Trader/             Demand-supply LONG/SHORT trade plans
│   │       ├── RSAgent/            Relative strength vs sector index
│   │       ├── QuantRisk/          Beta (date-aligned) / VaR / drawdown
│   │       ├── PromoterIntel/      Pledge + shareholding (PSU short-circuit)
│   │       ├── CatalystQuality/    Event and news quality classification
│   │       ├── TrendFlow/          FII/DII flows + bulk/block deals
│   │       └── OptionChain/        PCR / max pain / call wall / put floor
│   ├── config/
│   │   ├── universe.csv            98 Indian financial stocks
│   │   ├── weights.json            Agent scoring weights
│   │   └── llm_catalog.json        Models shown in UI
│   ├── graph/
│   │   ├── builder.py              Plain-asyncio _StockGraph + _run_with_retry
│   │   └── runner.py               Universe-level concurrency
│   ├── mcp_server/tools/           All data-fetching tools (MCP-compatible)
│   │   ├── market.py               FYERS candles (in-process cache)
│   │   ├── fundamentals.py         NSE XBRL + Yahoo ratios (7-day cache)
│   │   ├── nse.py                  Events/deals/shareholding/pledge/FII-DII
│   │   ├── quant_risk.py           Beta (date-aligned, guarded) + VaR
│   │   └── indianapi.py            Premium news (once-daily, shared)
│   ├── scoring/
│   │   ├── baselines.py            Deterministic baseline per agent
│   │   └── policy.py               Weighted score + consensus + completeness
│   ├── service/models/
│   │   ├── llm_factory.py          get_llm() with run-time config override
│   │   ├── model_config.json       Bulk: nemotron-bolt everywhere
│   │   └── model_config.mixed.json Deep-dive: qwen3 + bolt + nano
│   ├── store/
│   │   ├── db.py                   SQLite schema + prune_old_data + db_stats
│   │   └── fundamentals.py         horizon_metrics (EPS split-adjusted)
│   ├── viz/
│   │   ├── chart.py                Static matplotlib PNG chart
│   │   └── interactive_chart.py    Plotly TradingView-style HTML chart
│   └── static/index.html           Complete SPA (sidebar + 4 tabs)
├── docs/
│   ├── ARCHITECTURE.md             Full system diagram + agent stories
│   └── TRADINGAGENTS_COMPARISON.md Data source comparison
├── tests/                          pytest suite (35 tests)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Tests

```bash
pytest -q --deselect "tests/test_pipeline.py::test_real_mcp_server_over_stdio_exposes_all_tools"
# Expected: 35 passed, pre-existing MCP + yfinance-eviction flakes excluded
```

---

## Known limitations

- **Internal thinking tokens** — reasoning models (QwQ, DeepSeek-R1) hide chain-of-thought tokens; token counts shown are an undercount for those models
- **FYERS token expires daily** — run `python -m scripts.fyers_login` each morning
- **NSE rate limits** — a daily gate prevents hammering NSE; use `FORCE_REFRESH=1` only when needed
- **IndianAPI quota** — 500 req/month; pipeline fetches market-wide news once per day, shared across all stocks
- **Nifty 500 coverage** — 232 symbols tracked; ~10-15 may have no Yahoo data on any given day (newly listed, suspended, etc.)

---

## Licence

Research / personal use only. Not financial advice.
