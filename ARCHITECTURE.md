# Architecture

Multi-agent research pipeline that scores ~98 NSE financial/banking stocks. Specialist agents read evidence from a
single shared tool server and each return a score in `-10..+10`; a Decision Maker reconciles them; **Python**, not an
LLM, computes the authoritative weighted score, applies hard gates, ranks the stocks and exports CSV + Excel.

Research only — nothing in the system can place an order (`PLACE_ORDER=false`).

---

## 1. High-level flow

```
                       ┌─────────────────────────────────────────────────────────────┐
                       │  LangGraph graph, built once per run (app/graph/builder.py)   │
   Universe (CSV)      │                                                               │
      │                │   START ─┬─> fundamental ─┐                                   │
      │  bounded       │          ├─> industry ────┼─> decision ─> finalize ─> END     │
      ▼  concurrency   │          └─> technical ───┘   (deterministic scoring)         │
  run_universe() ──────┤               (indicator + investment_trend + option_chain,   │
 (app/graph/runner.py) │                run concurrently inside the technical node)    │
      │                └─────────────────────────────────────────────────────────────┘
      │
      ├─ refresh stale datasets once (app/jobs/refresh.py)
      ├─ per stock: graph.ainvoke(...)  ── one failing stock never stops the run
      ├─ policy.rank(results)           ── deterministic ordering
      ├─ run_store.save_run(...)        ── persist every agent's I/O + tokens
      ├─ progress.finish_run(...)       ── finalise live progress + token totals
      └─ export(...)                    ── CSV + XLSX

  All agents call tools on ONE shared MCP server (app/mcp_server/server.py), either in-process or over stdio.
  Live per-agent status and token usage are streamed to the dashboard while the graph runs.
```

The authoritative score is **never** produced by an LLM. Specialists and the Decision Maker only interpret evidence;
`app/scoring/policy.py` owns the weighted score, the direction/data/trade-status gates, and ranking.

---

## 2. Agents

| Agent | Node | Tools (allow-listed) | Output |
|---|---|---|---|
| Fundamental | `fundamental` | `nse_company_events`, `quarterly_fundamentals`, `yf_fundamentals`, `news_search` | score + reasoning + evidence |
| Industry | `industry` | `rbi_policy`, `foreign_rates`, `macro_snapshot`, `policy_calendar`, `news_search` | score (shared per day across all financials) |
| Indicator | `technical` | `get_technical_indicators`, `get_candles` | score |
| Investment Trend (TrendFlow) | `technical` | `nse_fii_dii`, `nse_deals`, `nse_shareholding` | score (capped ±3 without stock-specific evidence) |
| Option Chain | `technical` | `get_option_chain` | score |
| Decision Maker | `decision` | — (reads the specialists' outputs) | reconciliation: direction view, proposed score, conflicts, risks, suggested trade status |

The three technical specialists run concurrently inside the technical node (`app/Agents/Technical/agent.py`).

Each specialist follows the same contract (`app/Agents/base.py::analyze`):

1. Call its allow-listed tools → evidence.
2. If there is **no usable evidence**, return status `unavailable` — the LLM is *not* asked to guess.
3. The LLM reads `prompt.md` + evidence and returns JSON (validated against `LLMScoreOutput`, one retry on bad JSON).
4. Code applies hard rules (score clamp to `[-10,10]`, caps, confidence normalisation) and builds an `AgentResult`.

This design is provider-agnostic: no provider-specific tool-calling or structured-output features are required, so the
same code runs on local Ollama/OLMX models and hosted OpenAI models.

---

## 3. Tools (shared MCP server)

Every tool returns a uniform envelope `{status: ok|unavailable|error, source, retrieved_at, data, ...}` and never
raises; a failed feed surfaces as `unavailable`/`error`, never as a neutral/zero value. Agents may call only the tools
in their own `TOOLS` allow-list (enforced by `app/mcp_server/client.py`).

| Tool | Source |
|---|---|
| `get_technical_indicators`, `get_candles` | FYERS v3 (Yahoo fallback) |
| `get_option_chain` | FYERS option chain (+ saved weekly/monthly OI snapshots) |
| `nse_company_events` | NSE announcements, board meetings, corporate actions |
| `quarterly_fundamentals` | NSE results filings (+ Yahoo fallback), stored in SQLite |
| `yf_fundamentals` | Yahoo Finance (current snapshot) |
| `nse_fii_dii`, `nse_deals`, `nse_shareholding` | NSE |
| `rbi_policy`, `foreign_rates`, `macro_snapshot`, `policy_calendar` | RBI feeds, FRED, Yahoo, calendar CSV |
| `news_search` | Google News RSS (SQLite cache) |

---

## 4. Deterministic scoring (`app/scoring/policy.py`)

- `final = 0.30·fundamental + 0.20·industry + 0.25·indicator + 0.15·investment_trend + 0.10·option_chain`
  (weights in `app/config/weights.json`, versioned).
- **Missing inputs are not zero.** The weighted score is computed over the available inputs with weights
  re-normalised; `data_completeness` reports the share of total weight actually present.
- **Direction gate:** all scores > 0 → `BULLISH_CONSENSUS`; all < 0 → `BEARISH_CONSENSUS`; mixed signs → `MIXED`; any
  zero → `NEUTRAL_OR_MIXED`.
- **Data gate:** required agents are `fundamental, industry, indicator`; completeness below `0.80` →
  `INSUFFICIENT_DATA` (the Decision Maker is skipped — nothing to reconcile).
- **Trade-status validation:** the LLM may *suggest* a status; code decides. `POTENTIAL_ENTRY` requires bullish
  consensus, enough data, score ≥ `min_final_score_for_entry`, and a non-extended setup, else it is downgraded with the
  reason recorded. Bearish consensus forces `AVOID`.
- LLM-proposed score vs the computed score: a gap beyond `score_discrepancy_threshold` is added to `risk_flags`.

---

## 5. Local data store (SQLite, `data/app.db`)

One file holds: 16-quarter results (`fundamentals_q`), news and board decisions (`news`, `company_events`,
de-duplicated by hash/ID), FII/DII daily rows, option-chain OI snapshots, the shared industry score, per-run agent I/O
(`run_agent_io`, including per-agent token usage), a small JSON cache (`kv`, including per-run token totals), and a
`refresh_log`.

- **Refresh on load:** at the start of a live run, stale datasets are refreshed once; anything already refreshed today
  is left alone. `--no-refresh` skips it; `--force-refresh` ignores the once-a-day gate. Historical `--as-of` runs are
  never refreshed.
- **Quarterly metrics are calendar-aligned.** QoQ and YoY are matched to the correct calendar quarter (YoY = the same
  quarter one year earlier), computed in `app/store/fundamentals.py::compute_metrics`. If the series has a hole, the
  affected comparison is `null` with a reason in `<metric>.null_reasons` rather than silently comparing the wrong
  quarters. Net margin's year-ago figure is the same quarter a year back. The operating-profit label (PPOP for lenders,
  EBITDA otherwise) appears only when those values actually exist.
- **Point-in-time:** backtests (`--as-of T`) only see quarters/events that were public by T.
- **Industry** is computed once per day for all financials and reused by every stock.

---

## 6. Observability: live progress + token telemetry

Two layers, both provider-agnostic:

1. **Structured logs.** `logs/runs/<run_id>.jsonl` records every tool call and every `llm_attempt` (provider/model,
   agent, duration, queue vs inference time, input/output chars and tokens, tokens/sec, and whether the token count is
   exact provider usage or a `chars/4` estimate). `logs/runs/<run_id>/traces/<SYMBOL>_<agent>.json` holds the exact
   prompt and raw reply per agent. Per-model totals are logged at run completion.

2. **Live registry** (`app/service/progress.py`). An in-memory, thread-safe, bounded (last 50 runs) registry tracks,
   per run × symbol × agent, the lifecycle `pending → running → done | unavailable | error | skipped` plus accumulated
   input/output tokens. Graph nodes and the technical coordinator call `agent_start`/`agent_finish`; `telemetry.py`
   calls `record_tokens` on every LLM attempt (keyed by the logging context vars). `GET /api/runs/{id}/progress`
   serves a snapshot for the dashboard. Per-agent tokens are also persisted into `run_agent_io` and run totals into
   `kv`, so the endpoint can reconstruct a completed run after a restart.

Retried LLM attempts accumulate in token totals (a malformed reply genuinely costs its tokens) — one reason
`LLM_JSON_OBJECT` matters on local models.

---

## 7. LLM provider wiring (`app/service/models/`)

- `model_config.json` maps each agent to `{provider, model, temperature?, max_tokens?}`; unspecified agents use
  `default`. The shipped default is local Ollama (`gemma3-12b-16k`).
- `llm_factory.py` builds one client per `(provider, model, ...)` spec (`lru_cache`). `ollama`, `olmx`, `openai` all
  speak the OpenAI chat protocol via `ChatOpenAI` with a different base URL; `stub` is the offline fake for tests/demos.
- **Local JSON mode:** for `ollama`/`olmx`, the client is built with `response_format={"type":"json_object"}` (toggle
  `LLM_JSON_OBJECT`), which removes most malformed-JSON re-asks. The OpenAI path is left untouched (it requires the word
  "json" in the prompt).
- **Local concurrency gate:** Ollama/OLMX clients are wrapped in `_SerializedOllamaModel`, a per-model semaphore
  (`OLLAMA_MAX_CONCURRENT_REQUESTS`) that keeps queue wait separate from inference time for trustworthy tokens/sec.
- **Context window:** a 16k-context model is shipped as an Ollama Modelfile
  (`app/config/ollama/gemma3-12b-16k.Modelfile`) because the largest agent prompt is ~8k tokens and Ollama's default
  window would silently truncate it. See the README's "Running on a local LLM" section.

---

## 8. API & dashboard (`app/api.py`, `app/static/index.html`)

- FastAPI app with an async run model: `POST /api/runs` schedules a background task and returns immediately; clients
  poll `GET /api/runs/{id}` for status and `GET /api/runs/{id}/progress` for the live agent matrix + token totals.
  Only one run is active at a time; in-memory bookkeeping is bounded. Startup uses a lifespan handler.
- Endpoints: `/health`, `/api/universe`, `/api/runs` (list/create), `/api/runs/{id}`, `/api/runs/{id}/progress`,
  `/api/runs/{id}/results` (`?valid_only=`), `/api/runs/{id}/agents`, `/api/runs/{id}/traces`.
- The single-file dashboard (vanilla JS, no CDN) provides stock selection, a **live agent matrix** (stock × agent
  status with per-agent score/tokens), a pipeline progress bar, a **token-calculation panel** (input/output/total +
  per-model breakdown), the ranked results table, CSV export, and a per-agent trace viewer.

---

## 9. Design principles

- **LLMs interpret; Python decides.** Scores, gates and ranking are deterministic and auditable.
- **Absence is explicit.** Missing data is `unavailable`/`null` with a reason, never a silent zero or a guessed value.
- **Isolate failures.** One failing tool, agent or stock degrades gracefully and never sinks the run.
- **Provider-agnostic.** The same pipeline runs on a local 12B and on hosted APIs with only a config change.
- **Research only.** No order placement anywhere in the system.
