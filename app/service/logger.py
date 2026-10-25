"""Logging for easy debugging.

* Console + rotating file (logs/app.log) with run_id / symbol / agent on every line.
* One JSONL file per run (logs/runs/<run_id>.jsonl) with every tool call and LLM call.
* Raw LLM prompts/responses per stock+agent in logs/runs/<run_id>/traces/ (DEBUG aid).
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
import warnings
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Iterator

from app.service.settings import get_settings

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
symbol_var: contextvars.ContextVar[str] = contextvars.ContextVar("symbol", default="-")
agent_var: contextvars.ContextVar[str] = contextvars.ContextVar("agent", default="-")

_configured = False
# Use a module logger directly; calling get_logger here would recurse into setup_logging().
log = logging.getLogger(__name__)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.symbol = symbol_var.get()
        record.agent = agent_var.get()
        return True


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    s = get_settings()
    log_dir = s.path(s.log_dir)
    (log_dir / "runs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(run_id)s|%(symbol)s|%(agent)s] %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, s.log_level.upper(), logging.INFO))
    for handler in (logging.StreamHandler(), RotatingFileHandler(log_dir / "app.log", maxBytes=5_000_000, backupCount=3)):
        handler.setFormatter(fmt)
        handler.addFilter(_ContextFilter())
        root.addHandler(handler)
    # yfinance's internal Series[0] usage triggers a known pandas positional-indexing
    # FutureWarning. It originates inside the dependency; this exact warning is deprecated
    # upstream and does not indicate a problem in our dataframe indexing.
    warnings.filterwarnings(
        "ignore",
        message=r"Series.__getitem__ treating keys as positions is deprecated.*",
        category=FutureWarning,
    )
    # yfinance currently uses pandas APIs with ``inplace=True`` that emit Pandas4Warning
    # on pandas 3.1+. Keep this narrowly scoped to those known dependency call sites.
    warnings.filterwarnings(
        "ignore",
        message=r"The inplace keyword in DataFrame\.(sort_index|set_index|drop|rename) is deprecated.*",
        category=Warning,
        module=r"yfinance(\..*)?",
    )
    # Third-party libraries often emit per-request diagnostics already captured by our
    # structured tool/LLM events. Keep the console focused on actionable application logs.
    for noisy in (
        "httpx", "httpcore", "urllib3", "mcp", "openai", "langchain", "langchain_core",
        # Browser polling produces one access line per UI refresh and obscures run diagnostics.
        "uvicorn.access",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # yfinance prints low-level crumb/HTTP failures before returning an exception; the
    # provider adapter records the structured failure, so suppress the duplicate line.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def _run_dir():
    s = get_settings()
    d = s.path(s.log_dir) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_event(kind: str, **fields: Any) -> None:
    """Append one structured record to the current run's JSONL file."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id_var.get(), "symbol": symbol_var.get(), "agent": agent_var.get(),
        "kind": kind, **fields,
    }
    try:
        with open(_run_dir() / f"{run_id_var.get()}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass  # logging must never break the run


def save_trace(name: str, payload: dict[str, Any]) -> None:
    """Dump a full prompt/response pair for one agent+stock."""
    d = _run_dir() / run_id_var.get() / "traces"
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{symbol_var.get()}_{name}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


@contextmanager
def log_context(*, run_id: str | None = None, symbol: str | None = None, agent: str | None = None) -> Iterator[None]:
    tokens = []
    for var, val in ((run_id_var, run_id), (symbol_var, symbol), (agent_var, agent)):
        if val is not None:
            tokens.append((var, var.set(val)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


@contextmanager
def timed() -> Iterator[dict[str, float]]:
    box: dict[str, float] = {}
    t0 = time.perf_counter()
    try:
        yield box
    finally:
        box["ms"] = round((time.perf_counter() - t0) * 1000, 1)


def log_llm_usage_summary(run_id: str) -> dict[str, dict[str, Any]]:
    """Aggregate per-attempt token telemetry for this run and emit one concise summary/model."""
    path = _run_dir() / f"{run_id}.jsonl"
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "input_tokens": 0, "output_tokens": 0, "duration_ms": 0.0,
        "queue_ms": 0.0, "inference_ms": 0.0, "successful_output_tokens": 0,
        "exact_calls": 0, "estimated_calls": 0,
    })
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") != "llm_attempt":
                continue
            key = f"{rec.get('provider', '?')}:{rec.get('model', '?')}"
            t = totals[key]
            t["calls"] += 1
            t["input_tokens"] += int(rec.get("input_tokens") or 0)
            t["output_tokens"] += int(rec.get("output_tokens") or 0)
            t["duration_ms"] += float(rec.get("duration_ms") or 0)
            t["queue_ms"] += float(rec.get("queue_ms") or 0)
            if rec.get("status") == "ok":
                t["inference_ms"] += float(rec.get("inference_ms") or rec.get("duration_ms") or 0)
                t["successful_output_tokens"] += int(rec.get("output_tokens") or 0)
            if rec.get("token_count_source") == "provider_usage":
                t["exact_calls"] += 1
            else:
                t["estimated_calls"] += 1
    except OSError:
        return {}
    summary: dict[str, dict[str, Any]] = {}
    for model, t in totals.items():
        inference_s = t["inference_ms"] / 1000
        wall_s = t["duration_ms"] / 1000
        item = {**t, "duration_ms": round(t["duration_ms"], 1), "queue_ms": round(t["queue_ms"], 1),
                "inference_ms": round(t["inference_ms"], 1),
                "tokens_per_second": round(t["successful_output_tokens"] / inference_s, 2) if inference_s else 0.0,
                "wall_tokens_per_second": round(t["output_tokens"] / wall_s, 2) if wall_s else 0.0}
        summary[model] = item
        log_event("llm_usage_summary", run_id=run_id, model_key=model, **item)
        log.info("LLM usage %s | calls=%d input_tokens=%d output_tokens=%d generation_tps=%.2f wall_tps=%.2f queue_s=%.2f token_counts=%d exact/%d estimated",
                 model, item["calls"], item["input_tokens"], item["output_tokens"], item["tokens_per_second"],
                 item["wall_tokens_per_second"], item["queue_ms"] / 1000,
                 item["exact_calls"], item["estimated_calls"])
    return summary
