"""Universe orchestration: bounded concurrency, one failed stock never stops the run, then rank + export."""
from __future__ import annotations

import asyncio
import csv
import uuid
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.graph.builder import build_graph
from app.jobs.refresh import format_report, refresh_stale
from app.ledger.events import EventLedger
from app.mcp_server.client import ToolClient
from app.mcp_server.tools.common import now_ist, parse_as_of
from app.schemas import StockResult
from app.scoring import policy
from app.service import progress
from app.service.logger import get_logger, log_context, log_event, log_llm_usage_summary
from app.service.settings import get_settings
from app.store import runs as run_store

log = get_logger(__name__)


def load_universe(path: str | None = None, only: list[str] | None = None, sub_sector: str | None = None) -> list[dict[str, str]]:
    s = get_settings()
    with open(s.path(path or s.universe_file), newline="") as fh:
        rows = list(csv.DictReader(fh))
    if only:
        wanted = {x.strip().upper() for x in only}
        rows = [r for r in rows if r["symbol"].upper() in wanted]
        missing = wanted - {r["symbol"].upper() for r in rows}
        if missing:
            raise ValueError(f"symbols not in universe file: {sorted(missing)}")
    if sub_sector:
        rows = [r for r in rows if r["sub_sector"].startswith(sub_sector)]
    return rows


last_refresh_report: dict[str, Any] = {}


async def run_universe(rows: list[dict[str, str]], as_of: str | None = None, run_id: str | None = None,
                       mode: str | None = None, concurrency: int | None = None, refresh: bool = False,
                       scope: str = "bulk") -> tuple[str, list[StockResult]]:
    run_started = time.perf_counter()
    s = get_settings()
    cfg = policy.load_config()
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    as_of = as_of or now_ist().isoformat(timespec="seconds")
    sem = asyncio.Semaphore(concurrency or s.max_concurrent_stocks)
    ledger = EventLedger()
    results: list[StockResult] = []
    progress.init_run(run_id, [r["symbol"] for r in rows])

    async with ToolClient(mode) as client:
        if refresh:
            with log_context(run_id=run_id):
                # Always-fresh: wipe the day-gate + volatile caches so this run refetches every tool live
                # (only for live runs; historical as_of reads stored data as-is).
                if s.always_fresh and parse_as_of(as_of).date() >= now_ist().date():
                    from app.store.db import reset_daily_caches
                    reset_daily_caches()
                last_refresh_report.clear()
                last_refresh_report.update(await refresh_stale(client, rows, as_of))
                log.info(format_report(last_refresh_report))
        graph = build_graph(client, cfg, ledger)

        async def one(row: dict[str, str]) -> StockResult:
            sym = row["symbol"]
            meta = {"symbol": sym, "company_name": row["company_name"], "sub_sector": row["sub_sector"]}
            async with sem:
                with log_context(run_id=run_id, symbol=sym):
                    log.info("start %s (%s)", sym, row["sub_sector"])
                    try:
                        out = await graph.ainvoke({"run_id": run_id, "symbol": sym, "meta": meta, "as_of": as_of, "results": {}})
                        return out["final"]
                    except Exception as exc:  # noqa: BLE001 - isolate failures per stock
                        log.exception("stock failed")
                        log_event("stock_error", error=f"{type(exc).__name__}: {exc}")
                        return policy.finalize(run_id=run_id, symbol=sym, meta=meta, as_of=as_of, results={}, decision=None, cfg=cfg,
                                               error=f"{type(exc).__name__}: {exc}")

        with log_context(run_id=run_id):
            log.info("run %s: %d stocks, as_of=%s, mode=%s", run_id, len(rows), as_of, client.mode)
            results = await asyncio.gather(*(one(r) for r in rows))
    ranked = policy.rank(list(results))
    run_store.save_run(run_id, as_of, scope, ranked)
    progress.finish_run(run_id, "completed")
    with log_context(run_id=run_id):
        # Observability must never turn a successfully scored run into a failed API run.
        try:
            log_llm_usage_summary(run_id)
        except Exception:  # noqa: BLE001 - metrics are best-effort, scoring is already complete
            log.exception("could not write LLM usage summary; preserving completed scoring results")
        log.info("run %s done: %d valid / %d total | wall_time_s=%.2f", run_id, sum(r.valid for r in ranked), len(ranked), time.perf_counter() - run_started)
    return run_id, ranked
