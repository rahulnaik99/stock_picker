"""Refresh-on-load: bring every locally stored dataset up to date, once.

Each tool decides by itself whether to touch the network:
  results   newest stored quarter < 70 days old                -> use SQLite, no fetch
  others    already refreshed today (refresh_log)              -> use SQLite, no fetch
  otherwise fetch, store only what is new, mark as refreshed today.
This job just calls those tools for the whole universe up front (bounded concurrency) and reports what happened.
`--force-refresh` (FORCE_REFRESH=1) ignores the once-a-day gate. Historical `--as-of` runs are never refreshed.

Standalone:  python -m app.jobs.refresh [--symbols SBIN HDFCBANK] [--force-refresh]
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from typing import Any

from app.Agents.Fundamental.agent import news_query
from app.Agents.Industry.agent import CFG as INDUSTRY_CFG
from app.mcp_server.client import ToolClient
from app.mcp_server.tools.common import now_ist, parse_as_of
from app.service.logger import get_logger

log = get_logger(__name__)


async def refresh_stale(client: ToolClient, rows: list[dict[str, str]], as_of: str | None = None, concurrency: int = 4) -> dict[str, Any]:
    if parse_as_of(as_of).date() < now_ist().date():
        return {"skipped": "historical as_of: stored data is read as-is, nothing is refreshed"}
    sem = asyncio.Semaphore(concurrency)
    tally: dict[str, Counter] = defaultdict(Counter)

    async def call(dataset: str, tool: str, **args: Any) -> None:
        async with sem:
            env = await client.call(tool, **args)
        tally[dataset][f"{env.get('status')}/{env.get('cache') or 'n/a'}"] += 1

    jobs = [call("fii_dii (market)", "nse_fii_dii", as_of=as_of)]
    jobs += [call("industry news", "news_search", query=q, days=14, as_of=as_of) for q in INDUSTRY_CFG["news_queries"]]
    for r in rows:
        sym = r["symbol"]
        jobs += [call("quarterly results", "quarterly_fundamentals", symbol=sym, as_of=as_of, sub_sector=r.get("sub_sector")),
                 call("events / board decisions", "nse_company_events", symbol=sym, days=120, as_of=as_of),
                 call("company news", "news_search", query=news_query(r.get("company_name") or sym), days=14, as_of=as_of),
                 call("option chain + OI snapshot", "get_option_chain", symbol=sym, strikecount=10, as_of=as_of)]
    await asyncio.gather(*jobs)
    report = {k: dict(v) for k, v in tally.items()}
    for k, v in report.items():
        log.info("refresh %-28s %s", k, v)
    return report


def format_report(report: dict[str, Any]) -> str:
    if "skipped" in report:
        return f"Refresh: {report['skipped']}"
    lines = ["Refresh (status/cache -> count; refreshed = fetched now, hit = served from SQLite, stale = fetch failed, stored data used):"]
    lines += [f"  {k:<28} " + ", ".join(f"{a}: {b}" for a, b in sorted(v.items())) for k, v in report.items()]
    return "\n".join(lines)


async def _main(symbols: list[str] | None) -> None:
    from app.graph.runner import load_universe

    async with ToolClient() as client:
        print(format_report(await refresh_stale(client, load_universe(only=symbols))))


if __name__ == "__main__":
    import os

    p = argparse.ArgumentParser(description="Refresh stale local datasets (once a day)")
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--force-refresh", action="store_true")
    a = p.parse_args()
    if a.force_refresh:
        os.environ["FORCE_REFRESH"] = "1"
        from app.service.settings import get_settings
        get_settings().force_refresh = True
    asyncio.run(_main(a.symbols))
