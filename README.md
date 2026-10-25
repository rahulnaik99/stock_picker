# Financial Stock Scoring (LangGraph)

Implements `TRADING_STOCK_SCORING_ARCHITECTURE.md` for ~98 NSE financial/banking stocks. Five specialist agents score each stock
(-10..+10), a Decision Maker reconciles them, **Python computes the authoritative weighted score**, then stocks are ranked and exported to CSV + Excel.
Research only. Nothing here can place an order (`PLACE_ORDER=false`).

```
Universe ─> per stock (LangGraph):  fundamental ─┐
                                    industry ────┼─> decision maker ─> deterministic scoring ─> rank ─> CSV / XLSX
                                    technical ───┘   (indicator + investment_trend + option_chain, concurrent)
All agents call tools on ONE shared MCP server (app/mcp_server/server.py).
```

## Quick start
```bash
pip install -r requirements.txt
cp .env.example .env            # add keys for the providers you use + FYERS_APP_ID / FYERS_ACCESS_TOKEN
python -m pytest -q             # 58 tests, fully offline
python -m scripts.dry_run       # offline end-to-end run (fake LLM + synthetic data) -> data/dry_run/
./run.sh --api                                         # FastAPI interface at http://127.0.0.1:8000
python -m app.main --symbols SBIN HDFCBANK RBLBANK       # CLI alternative
python -m app.main --sub-sector bank                     # all banks
python -m app.main --as-of 2026-10-20                    # analysis time T (point-in-time filtering)
python -m app.main --stub --mode inprocess               # fake LLM, real tools
```
Outputs: `data/output/ranking_<run>.csv`, `data/output/stock_scores_<run>.xlsx` (Rankings, Agent Scores, Events, Data Quality, Configuration), `data/events.csv` (event ledger).

## FastAPI interface

Start the REST API (after installing requirements and configuring `.env`):

```bash
./run.sh --ui                         # JavaScript dashboard + API
# If packages are already installed and you are offline:
./run.sh --ui --no-install
# Choose another local port:
./run.sh --ui --port=8001
# Or directly:
.venv/bin/python -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the JavaScript dashboard. It supports stock search/filtering, selecting individual stocks or all visible matches, launching a scoring run, polling progress, viewing recent runs and rankings, and exporting displayed results to CSV. Open `http://127.0.0.1:8000/docs` for interactive Swagger documentation. The API starts runs asynchronously so long scoring runs do not hold the request open:

- `GET /health` — health check.
- `GET /api/universe` — configured stock universe; optional `sub_sector` and `limit` filters.
- `POST /api/runs` — start a scoring run. JSON body example: `{"symbols":["SBIN","HDFCBANK"],"refresh":true}`. For an offline demo use `{"symbols":["SBIN"],"stub":true,"mode":"inprocess","refresh":false}`.
- `GET /api/runs/{run_id}` — poll status.
- `GET /api/runs/{run_id}/results` — ranked output once complete; `?valid_only=true` filters invalid/ineligible rows.
- `GET /api/runs/{run_id}/agents?symbol=SBIN` — agent scores, evidence, provenance and reasoning.
- `GET /api/runs` — recent runs.

Only one scoring run is allowed at a time in a single API process. The API is bound to localhost by default; do not expose it publicly without authentication and deployment hardening. It is research-only and does not place orders. The CLI remains available as an alternative.

## OpenAI models per agent
All production LLM agents use the official OpenAI API. Configure `OPENAI_API_KEY` in `.env` and edit
`app/service/models/model_config.json` to choose a supported OpenAI model for each agent:
- `fundamental`: `gpt-4o` (deeper financial analysis)
- `industry`, `indicator`, `trend_flow`: `gpt-4o-mini` (lower-cost interpretation)
- `option_chain`, `decision_maker`: `gpt-4o` (options analysis and final synthesis)

The dashboard also lets you select **Ollama**, **OLMX**, or **OpenAI** for an individual run and enter an exact model ID. OLMX is treated as an OpenAI-compatible endpoint. Defaults are `OLMX_BASE_URL=http://127.0.0.1:8008/v1` and an API key from `OLMX_API_KEY`. Set those variables in the environment before starting the API; do not put secrets in the UI or source code. Per-run provider/model overrides are temporary and do not rewrite `model_config.json`. The `stub` provider is reserved for offline tests and demo runs.
Agent keys are `fundamental, industry, indicator, trend_flow, option_chain, decision_maker`; unspecified agents use `default`.

Example environment configuration for OLMX:
```bash
export OLMX_BASE_URL="http://127.0.0.1:8008/v1"
export OLMX_API_KEY="your-actual-key"
./run.sh --ui --no-install
```
In the dashboard choose **OLMX (OpenAI-compatible)** and enter the exact model ID advertised by your OLMX server. If OLMX returns HTTP 507, reduce concurrency and/or model context/output settings on the OLMX server; changing the provider dropdown alone cannot fix server memory exhaustion.

## Layout
```
app/
  main.py                      CLI
  service/settings.py logger.py models/{llm_factory.py, model_config.json}
  Agents/<Agent>/agent.py prompt.md        Fundamental, Industry, Technical/{Indicator,TrendFlow,OptionChain}, DecisionMaker
  mcp_server/server.py client.py registry.py tools/*.py     the one MCP server and its tools
  graph/builder.py runner.py   LangGraph per stock; universe loop with bounded concurrency
  scoring/policy.py            weights, direction gate, data-quality gate, trade-status validation, ranking
  ledger/events.py             upsert CSV event ledger (file-locked, point-in-time reads)
  export/writer.py             CSV + Excel
  config/                      universe.csv weights.json sectors.json foreign_rates.json policy_rates.json macro_events.csv
SOURCES_CATALOG.md             every stock, shared URLs, what to fetch per sub-sector
```

## Tools (all on the shared MCP server)
| Tool | Source | Used by |
|---|---|---|
| `get_technical_indicators`, `get_candles` | FYERS v3 (Yahoo fallback) | Indicator |
| `get_option_chain` | FYERS option chain (+ own weekly/monthly OI snapshots) | OptionChain |
| `nse_company_events` | NSE announcements, board meetings, corporate actions | Fundamental |
| `yf_fundamentals` | Yahoo Finance | Fundamental |
| `nse_fii_dii` (market-wide), `nse_deals`, `nse_shareholding` (stock-specific) | NSE | TrendFlow |
| `rbi_policy`, `foreign_rates`, `macro_snapshot`, `policy_calendar` | RBI feeds, FRED, Yahoo, calendar CSV | Industry |
| `news_search` | Google News RSS | Fundamental, Industry |

Each agent can call only the tools in its own `TOOLS` list. Every tool returns `{status: ok|unavailable|error, source, retrieved_at, data}` and never raises.

## Scoring rules (`scoring/policy.py`, `config/weights.json`)
- `final = 0.30 fundamental + 0.20 industry + 0.25 indicator + 0.15 investment_trend + 0.10 option_chain`, weights in JSON, versioned.
- Missing inputs are **not zero**: the score is computed over available inputs (weights re-normalised) and `data_completeness` is reported. Required: fundamental, industry, indicator; completeness below 0.80 = `INSUFFICIENT_DATA`.
- Direction gate: all scores > 0 `BULLISH_CONSENSUS`; all < 0 `BEARISH_CONSENSUS`; mixed signs `MIXED`; any zero `NEUTRAL_OR_MIXED`.
- `trade_status` suggested by the LLM is validated by code: `POTENTIAL_ENTRY` needs bullish consensus, enough data, score >= 2.0 and a non-extended setup; otherwise downgraded with the reason recorded.
- LLM-proposed score vs computed score: gap > 2 is flagged in `risk_flags`.
- FII/DII data is market-wide; without stock-specific evidence the TrendFlow score is capped at +/-3.

## Debugging
- `logs/app.log`: every line has `[run_id|symbol|agent]`; `LOG_LEVEL=DEBUG` for more.
- `logs/runs/<run_id>.jsonl`: every tool call (args, status, ms) and LLM call (provider, model, ms).
- `logs/runs/<run_id>/traces/<SYMBOL>_<agent>.json`: exact prompt and raw reply for each agent.
- Excel `Data Quality` sheet lists every unavailable/errored source and every status adjustment.

## Known limits (verify on your machine)
- NSE endpoints are unofficial and bot-protected: if blocked, set `NSE_COOKIES` from your browser. Failed feeds show as `unavailable`/`error`, never as neutral.
- RBI repo rate is not on a stable machine-readable endpoint: keep `config/policy_rates.json` current (shipped as `null`, which the Industry agent reports as "not verified").
- FYERS option-chain field names follow the v3 response shape and are covered by a fixture test, but have not been run against the live API from here. FYERS tokens expire daily.
- Weekly/monthly OI change needs saved daily snapshots, so it is empty until the app has run on several days.
- Yahoo fundamentals are a current snapshot (not point-in-time); bank NIM/NPA must come from filings.
- Initial weights and thresholds are hypotheses: validate with a walk-forward backtest before trusting rank for trading.

## Local data store (SQLite, `data/app.db`)
One file holds: 16-quarter results (`fundamentals_q`), news and board decisions (`news`, `company_events`, de-duplicated by hash/ID),
FII/DII daily rows, option-chain OI snapshots, the shared industry score, per-run agent I/O, and a `refresh_log`.

- **Refresh on load:** at the start of a run, stale datasets are refreshed once; anything already refreshed today is not touched again. `--no-refresh` skips it, `--force-refresh` ignores the once-a-day gate.
- **Results (70-day rule):** if the newest stored quarter is under 70 days old, no fetch happens. Otherwise NSE results filings and Yahoo are fetched and merged (one source per series, never mixed). Python computes QoQ (last 6), YoY (last 4), streaks and margin; the LLM gets the quarterly table plus these numbers. Banks/NBFCs use PPOP in place of EBITDA (labelled).
- **If NSE blocks you:** load your own history: `python -m app.jobs.import_results results.csv` (columns in the file's docstring). Same for FII/DII backfill: `python -m app.jobs.import_fii_dii flows.csv`.
- **Industry agent:** runs once per day for all financials (`ALL_FINANCIALS`), cached; every stock reuses that score.
- **Technical:** EMA 20/50/200 and RSI14 on 15m, 1h and daily candles with an agreement check; FII/DII 1/5/20-day net flows and streaks from SQLite; option-chain support/resistance from highest put/call OI strikes plus saved daily snapshots.
- **Saved runs:** every agent's source, reason, score, context used and missing data is stored per run. View with `python -m app.show_run <run_id> [--symbol SBIN]`; list runs from the same module. (No UI yet, by your choice.)

## LLM performance telemetry

Every LLM invocation appends an `llm_attempt` record to `logs/runs/<run_id>.jsonl`. Fields include provider/model, agent, duration, input/output characters, input/output token counts, generated tokens/second, model-inference time, queue-wait time, and the token-count source. Counts are exact when the provider returns usage metadata; otherwise they are explicitly labelled as estimates based on characters/4. At run completion, the app logs per-model totals and overall wall-clock runtime.

For local Ollama inference, requests are serialized per model by default (`OLLAMA_MAX_CONCURRENT_REQUESTS=1`) to avoid concurrent generations competing for unified memory/CPU. Ollama retries default to zero (`OLLAMA_MAX_RETRIES=0`): a timed-out request fails once rather than waiting through long repeated retry cycles. Adjust `LLM_TIMEOUT_SECONDS` and these settings only after checking the measured token throughput and prompt sizes.
