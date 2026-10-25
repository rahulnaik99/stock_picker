"""Backfill FII/DII history from a CSV so the 5- and 20-day analytics work from day one.

  python -m app.jobs.import_fii_dii flows.csv
Columns: date (e.g. 2026-10-20), category (FII or DII), buy_cr, sell_cr, net_cr (INR crore). Re-importing the same day just updates it.
"""
from __future__ import annotations

import sys

from app.store import flows

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(f"imported {flows.import_csv(sys.argv[1])} FII/DII rows")
