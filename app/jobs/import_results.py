"""Load your own quarterly-results history (e.g. a Screener/Excel export saved as CSV) into SQLite.

  python -m app.jobs.import_results results.csv
Columns: symbol, period_end (YYYY-MM-DD), revenue, op_profit, pbt, pat, eps; optional: basis (consolidated|standalone), op_kind (EBITDA|PPOP).
Values in INR crore. This source wins over NSE/Yahoo (it is yours), and is the fix when NSE blocks automated access.
"""
from __future__ import annotations

import sys

from app.store import fundamentals as fq

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(f"imported {fq.import_csv(sys.argv[1])} quarterly rows")
