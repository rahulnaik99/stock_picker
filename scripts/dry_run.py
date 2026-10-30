"""Offline end-to-end dry run: synthetic tool data + fake LLM. Proves the graph, scoring, ledger and export work.

    python -m scripts.dry_run
Outputs go to data/dry_run/ (a separate events ledger and output folder).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.export.writer import export  # noqa: E402
from app.graph import runner  # noqa: E402
from app.mcp_server import registry  # noqa: E402
from app.service.settings import get_settings  # noqa: E402
from tests.fakes import fake_tools  # noqa: E402


def main() -> None:
    s = get_settings()
    s.llm_force_provider, s.events_csv, s.output_dir, s.mcp_mode = "stub", "data/dry_run/events.csv", "data/dry_run", "inprocess"
    s.db_path = "data/dry_run/app.db"     # separate SQLite file: the dry run never touches your real stored data
    registry.TOOLS.update(fake_tools())
    rows = runner.load_universe(only=["SBIN", "HDFCBANK", "ICICIBANK", "RBLBANK", "BAJFINANCE", "HDFCLIFE", "BSE"])
    run_id, ranked = asyncio.run(runner.run_universe(rows, as_of="2026-10-20", scope="dry_run"))
    paths = export(ranked, run_id)
    for r in ranked:
        print(f"{r.symbol:<11} score={r.final_score!s:>6} {r.direction:<18} {r.trade_status:<17} data={r.data_status}")
    print(paths)
    print(f"saved agent I/O: python -c \"from app.service.settings import get_settings as g; g().db_path='data/dry_run/app.db'; import app.show_run as s; s.main(['{run_id}', '--symbol', 'SBIN'])\"")


if __name__ == "__main__":
    main()
