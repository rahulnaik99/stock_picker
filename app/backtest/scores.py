"""Point-in-time score functions for the backtest.

Each takes a symbol's full daily candle DataFrame plus an as-of timestamp and returns the score that the live
pipeline would have produced on that date, using ONLY candles up to and including the as-of bar (no look-ahead).

`indicator_score_asof` replays the exact deterministic `indicators.compute()` used in production, returning its
daily `rule_score` (-10..+10). The live tool adds a +/-1 multi-timeframe bonus from intraday candles; that bonus
is NOT reconstructable from daily history, so the backtest scores the daily-only rule_score (a clean, honest
subset). The LLM's +/-2 adjustment is likewise not replayed - this harness validates the deterministic backbone,
which is what the LLM only nudges.
"""
from __future__ import annotations

import pandas as pd

from app.mcp_server.tools import indicators

MIN_BARS = 60
WINDOW = 420


def indicator_score_asof(daily: pd.DataFrame, asof: pd.Timestamp,
                         min_bars: int = MIN_BARS, window: int = WINDOW) -> float | None:
    """Daily indicator rule_score as of `asof`, or None if there is too little history to compute it."""
    d = daily[daily["time"] <= asof]
    if len(d) < min_bars:
        return None
    d = d.tail(window).reset_index(drop=True)
    try:
        out = indicators.compute(d, None)
    except Exception:  # noqa: BLE001 - a bad/short window is simply "no score for this date", never a crash
        return None
    rs = out.get("rule_score")
    return float(rs) if rs is not None else None
