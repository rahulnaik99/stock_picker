"""Backtest harness for the deterministic scoring backbone.

Does the score actually predict forward returns? This package replays a point-in-time score for each
(symbol, date) over history and measures its predictive power against forward returns, with no look-ahead:
the score at date t is computed ONLY from candles up to and including t, and is tested against the return
from t to t+h.

Modules:
- engine   : pure statistics (forward returns, Spearman IC, cross-sectional buckets, long-short, sign split).
             No I/O, no network - fully unit-tested with synthetic panels.
- scores   : point-in-time score functions (indicator rule_score replayed from history).
- data     : the only networked piece - loads daily OHLCV from Yahoo into an in-memory panel.
- report   : renders the summary to Markdown and writes the per-pick CSV.
- __main__ : CLI (`python -m app.backtest ...`).
"""
from __future__ import annotations

from app.backtest.engine import build_records, cross_sectional_ic, spearman, summarize

__all__ = ["build_records", "cross_sectional_ic", "spearman", "summarize"]
