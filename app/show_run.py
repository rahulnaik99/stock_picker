"""Look at saved runs (bulk or specific): per stock, per agent: source, reason, score, context used.

  python -m app.show_run                         list saved runs
  python -m app.show_run RUN_ID                  one line per stock (final result)
  python -m app.show_run RUN_ID --symbol SBIN    full detail for one stock: every agent's I/O
  python -m app.show_run RUN_ID --symbol SBIN --context     also print the exact context the LLM received
"""
from __future__ import annotations

import argparse
import json
import textwrap

from app.store import runs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Show saved runs")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--symbol")
    p.add_argument("--context", action="store_true", help="print the full context sent to each agent's LLM")
    a = p.parse_args(argv)
    if not a.run_id:
        for r in runs.list_runs():
            print(f"{r['run_id']}  as_of={r['as_of']}  stocks={r['n_stocks']}  scope={r['scope'][:60]}  saved={r['created_at']}")
        return 0
    rows = runs.load_run(a.run_id, a.symbol)
    if not rows:
        print("No saved rows for that run/symbol.")
        return 1
    if not a.symbol:
        print(f"{'symbol':<12}{'score':>7}  {'direction':<18}{'trade status':<18}{'data':<9}rank")
        for r in rows:
            if r["agent"] == "final":
                e = r["extra"] or {}
                print(f"{r['symbol']:<12}{(r['score'] if r['score'] is not None else float('nan')):>7.2f}  {str(e.get('direction')):<18}"
                      f"{str(e.get('trade_status')):<18}{str(e.get('data_status')):<9}{e.get('rank')}")
        return 0
    for r in rows:
        print(f"\n=== {r['symbol']} / {r['agent']}  status={r['status']}  score={r['score']}  confidence={r['confidence']}  model={r['provider']}/{r['model']}")
        print("Reason :", textwrap.shorten(r["reason"] or "", 600))
        for s in (r["sources"] or []):
            print(f"Source : {s.get('tool')} [{s.get('status')}/{s.get('cache') or '-'}] {s.get('source')} @ {s.get('retrieved_at')} {s.get('message') or ''}")
        if r["missing"]:
            print("Missing:", r["missing"])
        if r["extra"]:
            print("Other  :", textwrap.shorten(json.dumps(r["extra"], default=str), 500))
        ctx = r["context_used"] or ""
        print("Context:", ctx if a.context else f"{len(ctx)} chars (add --context to print)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
