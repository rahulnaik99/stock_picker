# Stock Scoring Pipeline

A local, offline-first AI-powered research tool for scoring Indian financial stocks (banks, NBFCs, insurance, AMCs). Runs entirely on your machine — no cloud API required for the LLM (Ollama).

> **Research only. No orders are placed.**

---

## What it does

1. Pulls live data from NSE, FYERS, Yahoo Finance, RBI, FRED, and news sources
2. Runs **10 specialist AI agents** concurrently per stock (fundamental, industry, technical, risk, promoter, catalyst, etc.)
3. A **Decision Maker** reconciles all scores into a final verdict: `POTENTIAL_ENTRY`, `WAIT_FOR_SETUP`, or `AVOID`
4. Displays a live agent matrix in the browser — watch each agent complete in real time

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for a full story-format explanation with diagrams.

---

## Quick Start

### Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Tested on macOS (M1 Air) |
| [Ollama](https://ollama.ai) | Local LLM server |
| FYERS account | For live candles + option chain |
| Git | For cloning |

### 1. Clone and install

```bash
git clone https://github.com/rahulnaik99/stock_picker.git
cd stock_picker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Pull the default model

```bash
ollama pull nemotron-bolt-e4b-16k
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env — minimum required:
#   FYERS_APP_ID=...
#   FYERS_ACCESS_TOKEN=...   (generate with: python -m scripts.fyers_login)
```

### 4. Start the server

```bash
uvicorn app.api:app --reload --port 8000
```

Open **http://localhost:8000** in your browser.

---

## UI Overview

```
┌──────────────────────┬────────────────────────────────┐
│  Build a scoring run │  Recent runs / live progress   │
│  • Stock selector    │  • Agent matrix (live update)  │
│  • LLM model picker  │  • Token usage + speed         │
│  • ⚡ Bulk / 🔬 Deep  │  • Decision Maker verdict      │
│  • Run button        │                                │
├──────────────────────┴────────────────────────────────┤
│  Ranked stocks table  (score · direction · completeness)│
├───────────────────────────────────────────────────────┤
│  Agent Trace Viewer  (prompts · raw LLM replies)       │
│  📊 Interactive chart  (TradingView-style, zoomable)   │
└───────────────────────────────────────────────────────┘
            ⭐ Watchlists panel  (slide from right)
            🔌 Tool Health drawer  (slide from bottom)
```

---

## Run Modes

### ⚡ Bulk Scan
Uses `nemotron-bolt-e4b-16k` for all agents. Single model stays resident in RAM — no swap overhead. Best for daily universe scans (all 98 stocks in ~15-20 min).

### 🔬 Deep Dive
Uses the **mixed config** (`model_config.mixed.json`):
- `qwen3-30b-a3b-16k` for Fundamental, Trader, Option Chain, Promoter Intel
- `nemotron-bolt-e4b-16k` for Industry, Catalyst, Investment Trend, Decision Maker
- `nemotron-nano-4b-16k` for Indicator, RS Agent, Quant Risk

Best for 1–5 stocks you're actively considering trading. Richer reasoning, specific number citations.

**Requires** `OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1` for safe sequential model loading.

---

## Watchlists

Create named lists of your favourite stocks (e.g. "PSU Banks", "NBFC Top 5", "My Longs"). Click the **⭐ Lists** tab on the right edge of the screen. Add stocks from any selection; run an entire watchlist with one click.

---

## Tool Health

Click **🟢 Tool Health** in the footer to open a panel showing green/red status for every data source: Ollama, FYERS, NSE, Yahoo Finance, RBI, FRED, IndianAPI, MLX/OLMX.

---

## Configuration

### `app/service/models/model_config.json`
Assigns LLM provider + model per agent. Change models here (no restart needed — edit and re-run).

### `app/config/weights.json`
Agent weights (must sum to 1.0). Controls how much each specialist contributes to the final score.

### `app/config/llm_catalog.json`
Model list shown in the UI model picker. Add new Ollama models here.

### `app/config/universe.csv`
Your stock universe. Columns: `symbol, company_name, isin, sub_sector, exchange`.

---

## Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for:
- What each of the 10 agents does in plain English
- Every data source and what it provides
- A full Mermaid flow diagram
- The two-tier Bulk/Deep-Dive strategy

---

## Project Structure

```
stock_scoring/
├── app/
│   ├── api.py                     FastAPI application
│   ├── Agents/
│   │   ├── Fundamental/           PAT/EPS/GNPA analyst
│   │   ├── Industry/              Macro/sector analyst
│   │   ├── Technical/
│   │   │   ├── Indicator/         EMA/RSI/MACD/zones
│   │   │   ├── Trader/            Demand-supply trade plans
│   │   │   ├── RSAgent/           Relative strength
│   │   │   ├── QuantRisk/         Beta/VaR/drawdown
│   │   │   ├── PromoterIntel/     Pledge + shareholding
│   │   │   ├── CatalystQuality/   Event/news classification
│   │   │   ├── TrendFlow/         FII/DII + deals
│   │   │   └── OptionChain/       PCR/max pain/call wall
│   │   └── DecisionMaker/         Final reconciliation
│   ├── config/
│   │   ├── universe.csv           Your stock universe (98 stocks)
│   │   ├── weights.json           Agent scoring weights
│   │   ├── llm_catalog.json       Available models for UI picker
│   │   └── ollama/                Modelfile templates
│   ├── graph/
│   │   ├── builder.py             Plain-asyncio orchestrator (no langgraph)
│   │   └── runner.py              Universe-level concurrency
│   ├── mcp_server/tools/          All data-fetching tools
│   ├── scoring/                   Baselines, policy, weights
│   ├── store/                     SQLite (fundamentals, news, events, runs)
│   └── viz/
│       ├── chart.py               Static matplotlib quad chart
│       └── interactive_chart.py   Plotly TradingView-style HTML chart
├── docs/
│   └── ARCHITECTURE.md            Full system diagram + story
├── scripts/                       CLI utilities (fyers_login, bench_nimble)
├── tests/                         pytest suite (163 tests)
├── .env.example                   Environment variable template
├── requirements.txt
└── README.md
```

---

## Tests

```bash
pytest -q --deselect "tests/test_pipeline.py::test_real_mcp_server_over_stdio_exposes_all_tools"
# Expected: 163+ passed, 1 pre-existing yfinance-eviction flake
```

---

## Known Limitations

- **Internal thinking tokens not counted** — reasoning models (QwQ, o3, DeepSeek-R1) use hidden chain-of-thought tokens not tracked here. Actual cost/tokens for those models will be higher than reported.
- **FYERS token expires daily** — run `python -m scripts.fyers_login` each morning to refresh.
- **NSE is rate-limited** — a daily cache gate (`refreshed_today`) prevents hammering NSE. Run with `--force-refresh` only when needed.
- **IndianAPI quota** — 500 requests/month. The pipeline fetches market-wide news once per day and shares it across all stocks.

---

## Licence

Research / personal use only. This tool does not constitute financial advice.
