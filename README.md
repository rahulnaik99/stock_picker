# Financial Stock Scoring (LangGraph)

A multi-agent research pipeline for ~98 NSE financial/banking stocks. Five specialist agents score each stock
(-10..+10), a Decision Maker reconciles them, **Python computes the authoritative weighted score**, then stocks are ranked and exported to CSV + Excel.
Research only. Nothing here can place an order (`PLACE_ORDER=false`). See **`ARCHITECTURE.md`** for the full design.

```
Universe ─> per stock (LangGraph):  fundamental ─┐
                                    industry ────┼─> decision maker ─> deterministic scoring ─> rank ─> CSV / XLSX
                                    technical ───┘   (indicator + investment_trend + option_chain, concurrent)
All agents call tools on ONE shared MCP server (app/mcp_server/server.py).
Live per-agent progress and per-agent token usage are streamed to the dashboard while a run executes.
```

## Quick start
```bash
pip install -r requirements.txt
cp .env.example .env            # local LLM by default; add FYERS_* (token via `python -m scripts.fyers_login`) / FRED_API_KEY as needed
python -m pytest -q             # 99 tests, fully offline
python -m scripts.dry_run       # offline end-to-end run (fake LLM + synthetic data) -> data/dry_run/
./run.sh --ui                                          # FastAPI + live dashboard at http://127.0.0.1:8000
python -m app.main --symbols SBIN HDFCBANK RBLBANK       # CLI alternative
python -m app.main --sub-sector bank                     # all banks
python -m app.main --as-of 2026-10-20                    # analysis time T (point-in-time filtering)
python -m app.main --stub --mode inprocess               # fake LLM, real tools
```
Outputs: `data/output/ranking_<run>.csv`, `data/output/stock_scores_<run>.xlsx` (Rankings, Agent Scores, Events, Data Quality, Configuration), `data/events.csv` (event ledger).

> The shipped default is a **local LLM via Ollama** (`nemotron-e4b-16k`). See [Running on a local LLM](#running-on-a-local-llm-ollama--gemma-3-12b) before your first real run.

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

Open `http://127.0.0.1:8000/` for the JavaScript dashboard. It supports stock search/filtering, selecting individual stocks or all visible matches, launching a scoring run, and — while the run executes — a **live agent view**: a matrix of every selected stock × agent (Fundamental, Industry, Indicator, Inv. Trend, Option Chain, Decision) showing each agent as Pending → Running → Done / No data / Skipped / Error, a pipeline progress bar, and a **live token-calculation panel** (input / output / total tokens, with a per-model breakdown). You can also review recent runs and rankings, inspect per-agent traces, and export displayed results to CSV. Open `http://127.0.0.1:8000/docs` for interactive Swagger documentation. The API starts runs asynchronously so long scoring runs do not hold the request open:

- `GET /health` — health check.
- `GET /api/universe` — configured stock universe; optional `sub_sector` and `limit` filters.
- `POST /api/runs` — start a scoring run. JSON body example: `{"symbols":["SBIN","HDFCBANK"],"refresh":true}`. For an offline demo use `{"symbols":["SBIN"],"stub":true,"mode":"inprocess","refresh":false}`. A per-run `llm_provider` (`ollama`/`olmx`/`openai`) and exact `llm_model` may be supplied.
- `GET /api/runs/{run_id}` — poll status.
- `GET /api/runs/{run_id}/progress` — **live** per-agent status matrix and token totals for the run (falls back to persisted rows + token totals once a run leaves memory). This powers the live dashboard view.
- `GET /api/runs/{run_id}/results` — ranked output once complete; `?valid_only=true` filters invalid/ineligible rows (the exported CSV now carries a `valid` column so this works after a restart too).
- `GET /api/runs/{run_id}/agents?symbol=SBIN` — agent scores, evidence, provenance, reasoning and per-agent token usage.
- `GET /api/runs/{run_id}/traces?symbol=SBIN` — exact prompt and raw model reply saved per agent (local debugging; prompts can contain market data — keep the dashboard private).
- `GET /api/runs/{run_id}/report?format=md|json[&symbol=SBIN]` — downloadable debug report bundling every agent's model **input** (system prompt + payload) and **output** (raw reply) plus scores/baseline/adjustment/tokens and the final verdict. The dashboard's trace viewer has "Export report (.md/.json)" buttons for this.
- `GET /api/runs` — recent runs.

Only one scoring run is allowed at a time in a single API process; in-memory run bookkeeping is bounded (last 50 runs). The API is bound to localhost by default; do not expose it publicly without authentication and deployment hardening. It is research-only and does not place orders. The CLI remains available as an alternative.

## LLM models per agent
The provider and model for each agent live in `app/service/models/model_config.json`. The shipped default runs **locally on Ollama** (`nemotron-e4b-16k` for every agent; the Decision Maker at temperature 0.0). Agent keys are `fundamental, industry, indicator, trend_flow, option_chain, decision_maker`; unspecified agents use `default`.

Supported providers: `ollama` (local), `olmx` (local, OpenAI-compatible), `openai` (hosted), and `stub` (offline tests/demos). To use OpenAI instead, set `OPENAI_API_KEY` in `.env` and change the `provider`/`model` fields (e.g. `gpt-4o` for `fundamental`/`option_chain`/`decision_maker`, `gpt-4o-mini` for the rest).

The dashboard also lets you pick **Ollama**, **OLMX**, or **OpenAI** for a single run and enter an exact model ID. OLMX defaults are `OLMX_BASE_URL=http://127.0.0.1:8008/v1` and `OLMX_API_KEY`; set them in the environment before starting the API — do not put secrets in the UI or source code. Per-run provider/model overrides are temporary and do not rewrite `model_config.json`. If OLMX returns HTTP 507, reduce concurrency and/or model context/output on the OLMX server.

## Running on a local LLM (Ollama / Gemma 3 12B)

The default `model_config.json` targets a **custom Ollama model `nemotron-e4b-16k`** (a 4B model — fast enough for a laptop — rebuilt with a 16k context window). Build it once:

```bash
ollama pull hf.co/abenzerps/Nemotron-3.5-Bolt-E4B-GSQ-RCO-GGUF:Q5_K_M
ollama create nemotron-e4b-16k -f app/config/ollama/nemotron-e4b-16k.Modelfile
ollama show nemotron-e4b-16k --modelfile | grep num_ctx   # confirm 16384
```

**Why a custom model and not the plain tag?** Ollama's default context is **4096 tokens**, but the agent prompts (Fundamental ~5k, Industry ~4k) plus their JSON answer don't fit, so calls fail with `exceeds the available context size (4096)` or get truncated (`LengthFinishReasonError`). **Critically, Ollama's OpenAI-compatible `/v1` endpoint ignores a per-call `num_ctx`** — so `OLLAMA_NUM_CTX` in `.env` only works on very recent Ollama builds. The reliable fix is to raise the window **on the server**: either bake `PARAMETER num_ctx 16384` into a Modelfile (as above — works on every version) or start Ollama with `OLLAMA_CONTEXT_LENGTH=16384`. The same `app/config/ollama/` folder also ships a `gemma3-12b-16k.Modelfile` if you prefer a larger model (slower on a laptop).

Local-inference settings (set in `.env` / the environment; tune only after checking measured throughput):
- `OLLAMA_NUM_CTX=16384` (default) — context requested per call. Honoured only by recent Ollama; otherwise use a Modelfile or `OLLAMA_CONTEXT_LENGTH` (above).
- `LLM_JSON_OBJECT=1` (default) — asks Ollama/OLMX to return a JSON object. Local models otherwise emit the occasional malformed reply, which forces a full validation re-ask (double the tokens + latency). Scoped to local providers; OpenAI/stub are unaffected.
- `OLLAMA_MAX_CONCURRENT_REQUESTS` — simultaneous generations **per model**. Keep at `1` on a laptop; concurrent generations contend for memory/compute and usually make total wall-time worse.
- `OLLAMA_MAX_RETRIES=0` (default) — a timed-out request fails once instead of waiting through long retry cycles.
- `OLLAMA_KEEP_ALIVE=30m` (set on the Ollama **server**) — keeps the model resident between agent calls; otherwise each run pays a model-reload stall (visible as queue time in the token panel).
- `LLM_TIMEOUT_SECONDS` — raise if a large model is slow on your hardware.

A big local model (e.g. `gemma3:12b`) on a laptop can take minutes per call and time out; a 4B model with a 16k context (the shipped default) is the pragmatic choice. Lower `num_ctx` to 8192 in the Modelfile if memory is tight — but never leave it at the 4096 default.

## Layout
```
app/
  main.py                      CLI
  api.py                       FastAPI app, async runs, live /progress endpoint, static dashboard
  static/index.html            dashboard: stock selection, live agent matrix, token panel, results, trace viewer
  service/settings.py logger.py progress.py   config, structured logging, live per-agent progress + token registry
  service/models/{llm_factory.py, telemetry.py, model_config.json}   provider wiring, token telemetry, per-agent models
  Agents/<Agent>/agent.py prompt.md        Fundamental, Industry, Technical/{Indicator,TrendFlow,OptionChain}, DecisionMaker
  mcp_server/server.py client.py registry.py tools/*.py     the one MCP server and its tools
  graph/builder.py runner.py   LangGraph per stock; universe loop with bounded concurrency + progress hooks
  scoring/policy.py            weights, direction gate, data-quality gate, trade-status validation, ranking
  store/{fundamentals.py,runs.py,news.py,events.py,db.py,...}   SQLite datasets, calendar-aligned metrics, run I/O
  ledger/events.py             upsert CSV event ledger (file-locked, point-in-time reads)
  export/writer.py             CSV + Excel
  config/                      universe.csv weights.json sectors.json ... ollama/gemma3-12b-16k.Modelfile
ARCHITECTURE.md                full system design
SOURCES_CATALOG.md             every stock, shared URLs, what to fetch per sub-sector
```

## Tools (all on the shared MCP server)
| Tool | Source | Used by |
|---|---|---|
| `get_technical_indicators`, `get_candles` | FYERS v3 (official `fyers-apiv3` SDK; no fallback) | Indicator |
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
- Market data (candles + option chain) comes **only** from FYERS v3 via the official `fyers-apiv3` SDK — there is no fallback provider. **FYERS access tokens expire daily** — regenerate with `python -m scripts.fyers_login` (needs `FYERS_APP_ID`, `FYERS_SECRET_ID`, `FYERS_REDIRECT_URI` in `.env`). If the token is invalid/expired, the Indicator and Option-Chain tools return an error and those agents report unavailable (by design — no silent substitute). FYERS option-chain field names follow the v3 response shape and are covered by a fixture test but have not been run against the live API from here.
- Weekly/monthly OI change needs saved daily snapshots, so it is empty until the app has run on several days.
- Yahoo fundamentals are a current snapshot (not point-in-time); bank NIM/NPA must come from filings.
- Initial weights and thresholds are hypotheses: validate with a walk-forward backtest before trusting rank for trading.

## Local data store (SQLite, `data/app.db`)
One file holds: 16-quarter results (`fundamentals_q`), news and board decisions (`news`, `company_events`, de-duplicated by hash/ID),
FII/DII daily rows, option-chain OI snapshots, the shared industry score, per-run agent I/O, and a `refresh_log`.

- **Always-fresh (default):** `ALWAYS_FRESH=1` (the default) makes every **live** run refetch all tools — at run start it resets the once-a-day gate and the volatile caches (option-chain snapshot, shared industry score), then the normal once-per-run refresh pulls everything live (data tools are fetched once per run, not once per call, so the Industry score is still computed only once). Set `ALWAYS_FRESH=0` to fall back to the once-a-day behaviour below. Historical `--as-of` runs always read stored data as-is and are never refetched.
- **Refresh on load:** with `ALWAYS_FRESH=0`, at the start of a run stale datasets are refreshed once and anything already refreshed today is not touched again; `--no-refresh` skips it, `--force-refresh` ignores the once-a-day gate.
- **Results (70-day rule):** with `ALWAYS_FRESH=0`, if the newest stored quarter is under 70 days old no fetch happens (always-fresh and `--force-refresh` bypass this). NSE results filings (`/api/corporates-financial-results`, called with a get-quote Referer so NSE doesn't bot-block it) are the authoritative source; Yahoo is the fallback — both Yahoo quarterly statements are merged so the newest reported quarter isn't missed — one source per series, never mixed. The tool reports `series_source` (`nse` vs `yahoo`) and, for a fresh NSE pull, `latest_filing` (period end, Audited/Un-Audited, broadcast date, XBRL link) so the agent can cite the filing. For lenders it also parses the results **XBRL** for asset-quality/capital KPIs (GNPA%, NNPA%, CRAR, interest earned/expended, derived NII, PCR) into `latest_filing.xbrl` — with a full `diagnostics` block (http status, facts parsed, which tag matched each KPI, what is missing, and a sample of tags seen) so a failed or partial parse is always visible in the debug report, never silent. Python computes **calendar-aligned** QoQ and YoY (YoY = the same quarter a year earlier; a gap yields `null` with a reason), streaks and net margin. The operating-profit label (PPOP for lenders / EBITDA otherwise) is only shown when those values exist.
- Note: the SQLite store is kept as an append log even in always-fresh mode because a few analytics need history (FII/DII 5/20-day net flows, weekly/monthly OI change, event point-in-time); it is overwritten/appended on each fresh pull rather than served stale.
- **If NSE blocks you:** load your own history: `python -m app.jobs.import_results results.csv` (columns in the file's docstring). Same for FII/DII backfill: `python -m app.jobs.import_fii_dii flows.csv`.
- **Industry agent:** runs once per day for all financials (`ALL_FINANCIALS`), cached; every stock reuses that score.
- **Technical:** EMA 20/50/200 and RSI14 on 15m, 1h and daily candles with an agreement check; FII/DII 1/5/20-day net flows and streaks from SQLite; option-chain support/resistance from highest put/call OI strikes plus saved daily snapshots.
- **Saved runs:** every agent's source, reason, score, context used, missing data and token usage is stored per run. Browse them in the dashboard (recent runs, rankings, live/replayed agent matrix, token totals, traces) or from the CLI with `python -m app.show_run <run_id> [--symbol SBIN]`.

## LLM performance telemetry

Every LLM invocation appends an `llm_attempt` record to `logs/runs/<run_id>.jsonl`. Fields include provider/model, agent, duration, input/output characters, input/output token counts, generated tokens/second, model-inference time, queue-wait time, and the token-count source. Counts are exact when the provider returns usage metadata; otherwise they are explicitly labelled as estimates based on characters/4. At run completion, the app logs per-model totals and overall wall-clock runtime.

The same usage is also surfaced live: `app/service/progress.py` keeps an in-memory per-agent registry (status + accumulated tokens, keyed by run/symbol/agent) that `GET /api/runs/{run_id}/progress` exposes for the dashboard. Per-agent token counts are persisted into `run_agent_io`, and the run's token totals into the `kv` table, so the dashboard can still show them after the process restarts. Note that retried LLM attempts add up in the totals — a malformed reply genuinely costs its tokens — which is one reason `LLM_JSON_OBJECT=1` (fewer re-asks) matters on local models.

For local Ollama inference, requests are serialized per model (`OLLAMA_MAX_CONCURRENT_REQUESTS`) to avoid concurrent generations competing for unified memory/CPU, and Ollama retries default to zero (`OLLAMA_MAX_RETRIES=0`). See [Running on a local LLM](#running-on-a-local-llm-ollama--gemma-3-12b) for the full set of local tuning knobs.
