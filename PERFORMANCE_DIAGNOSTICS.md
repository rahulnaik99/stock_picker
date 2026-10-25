# Logging, compatibility, and LLM latency changes

## Findings from the supplied run (`20261023-205248-8414`)

The run completed in **42 min 27 sec**. The evidence points to local LLM calls—not market-data retrieval—as the dominant latency source:

| Agent | Recorded LLM duration | Prompt size (system + user characters, from saved trace) |
|---|---:|---:|
| fundamental | 23 min 06.8 sec | 30,695 |
| industry | 41 min 22.2 sec | 16,253 |
| investment_trend | 41 min 40.9 sec | 4,385 |
| indicator | 29.5 sec | 4,365 |
| option_chain | 14.5 sec | 3,706 |
| decision_maker | 38.3 sec | 15,253 |

Market-data calls in the same run were generally milliseconds to a few seconds; `nse_deals` was the slowest recorded data tool at about 7.6 sec and returned NSE HTTP 503 errors. That is worth monitoring, but it does not explain a 42-minute run.

The log shows `fundamental`, `industry`, and `investment_trend` running against the same local Ollama model concurrently. The SDK then logged retries around 20 and 40 minutes into the run. This is consistent with long request timeouts/retries combined with local inference contention; the exact configured timeout from the user's local `.env` was not included in the archive, so the timeout override cannot be confirmed from source alone. The fundamental prompt is also much larger than the other specialist prompts, but prompt size alone does not explain why the smaller industry/trend prompts took over 40 minutes.

## Changes made

- **Ollama concurrency:** serialize inference requests per model by default (`OLLAMA_MAX_CONCURRENT_REQUESTS=1`) so several graph branches do not compete for the same local model's RAM/CPU at once. This is configurable.
- **Retry behavior:** Ollama retries default to zero (`OLLAMA_MAX_RETRIES=0`), preventing a slow local request from entering multiple long timeout/retry cycles. The normal request timeout remains 120 seconds by default. OpenAI/cloud-provider retry settings remain independently configurable.
- **Token/performance telemetry:** every model invocation writes an `llm_attempt` JSONL event with agent, provider/model, elapsed time, queue wait, inference time, input/output characters, input/output tokens, generated tokens/sec, status, and token-count source. Provider usage metadata is used when available; otherwise token counts are explicitly marked as character-based estimates (`estimated_chars_div_4`). Run completion logs per-model totals and wall-clock duration.
- **Evidence truncation:** oversized evidence is compacted as valid JSON instead of slicing a serialized JSON string mid-token. The original prompts and tool definitions were not changed.
- **Logging noise:** routine model-config logs are DEBUG; OpenAI SDK retry INFO messages and duplicate low-level HTTP/library messages are suppressed in favor of structured application events. Repeated FYERS authentication warnings are emitted once per process, with the Yahoo fallback retained. The specific pandas 3.1 `Pandas4Warning` messages raised inside yfinance for deprecated `inplace=True` calls are filtered, and pandas is constrained to `<3.1` for compatibility. Uvicorn access-log polling is disabled for the UI launch and suppressed by the application logger if the API is launched directly; application errors and run timings remain logged.
- **Run-completion failure:** fixed a missing module logger (`NameError: name 'log' is not defined`) in the token-summary function. Also made token-summary logging best-effort, so telemetry/reporting failures cannot turn an otherwise successful scoring run into an API failure or cause completed-run endpoints to return 404.
- **Configuration/test mismatch:** updated the shipped-model test to expect the model actually configured in `model_config.json` (`qwen3-4b-instruct-16k:latest`) rather than the stale `qwen3:4b-instruct` value. Added the missing `.env.example` file that `run.sh` already expected.

## Where to read the metrics

- `logs/runs/<run_id>.jsonl`: per-call `llm_attempt` records and `llm_usage_summary` totals.
- `logs/app.log`: concise per-model usage totals and overall run wall time.

Look first at `queue_ms` vs `inference_ms` and `tokens_per_second`:

- High `queue_ms`: concurrency/serialization wait is significant.
- High `inference_ms` with low `tokens_per_second`: model inference, context size, or hardware/model configuration is the bottleneck.
- Repeated failed `llm_attempt` records with large duration and zero generated tokens: timeout/connection/model-server issue.
- Large input token counts: evidence/prompt size may need reducing without changing the scoring constraints.

## Validation

The changed modules compile. **33 focused tests passed**, including a new test covering per-model token-summary aggregation. The full suite could not be collected in this environment because required project dependencies (`langgraph`, `feedparser`, `mcp`, and `langchain-openai`) are not installed. No live Ollama performance benchmark was possible here; rerun one stock on the target Mac and inspect the new JSONL metrics to verify actual throughput.
