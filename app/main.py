"""CLI.

  python -m app.main                                  # whole financial universe, models from model_config.json
  python -m app.main --symbols SBIN HDFCBANK RBLBANK  # a few stocks
  python -m app.main --sub-sector bank --as-of 2026-10-20
  python -m app.main --stub --mode inprocess          # offline dry run (fake LLM, no network keys needed)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.export.writer import export
from app.graph import runner
from app.graph.runner import load_universe, run_universe
from app.jobs.refresh import format_report
from app.service import logger
from app.service.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Financial-stock scoring with LangGraph")
    p.add_argument("--symbols", nargs="*", help="subset of the universe file")
    p.add_argument("--sub-sector", help="prefix filter, e.g. bank, nbfc, insurance")
    p.add_argument("--as-of", help="analysis time T (ISO date or datetime, IST). Default: now")
    p.add_argument("--mode", choices=["stdio", "inprocess"], help="override MCP_MODE")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--no-refresh", action="store_true", help="skip the refresh-on-load step (use stored data as-is)")
    p.add_argument("--force-refresh", action="store_true", help="ignore the once-a-day gate and re-fetch every dataset")
    p.add_argument("--stub", action="store_true", help="use the offline fake LLM for every agent (dry run)")
    args = p.parse_args(argv)

    logger.setup_logging()
    if args.stub:
        get_settings().llm_force_provider = "stub"
    rows = load_universe(only=args.symbols, sub_sector=args.sub_sector)
    if not rows:
        print("No stocks selected.", file=sys.stderr)
        return 2
    if args.force_refresh:
        os.environ["FORCE_REFRESH"] = "1"          # inherited by the MCP server process
        get_settings().force_refresh = True
    scope = "specific:" + ",".join(r["symbol"] for r in rows) if (args.symbols or args.sub_sector) else "bulk"
    run_id, ranked = asyncio.run(run_universe(rows, as_of=args.as_of, mode=args.mode, concurrency=args.concurrency,
                                              refresh=not args.no_refresh, scope=scope))
    if runner.last_refresh_report:
        print(format_report(runner.last_refresh_report))
    paths = export(ranked, run_id)
    print(f"\nRun {run_id}: {sum(r.valid for r in ranked)} ranked / {len(ranked)} stocks")
    for r in ranked[:10]:
        print(f"  {'#' + str(i) if (i := ranked.index(r) + 1) and r.valid else '--':>4} {r.symbol:<12} {r.final_score!s:>6} {r.direction:<18} {r.trade_status}")
    print(f"CSV : {paths['csv']}\nXLSX: {paths['xlsx']}\nLogs: logs/app.log, logs/runs/{run_id}.jsonl\nSaved: every agent's source/reason/score/context -> python -m app.show_run {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
