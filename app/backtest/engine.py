"""Pure backtest statistics. No network, no file I/O, no LLM - every function is deterministic and unit-tested.

Terminology
-----------
- panel      : dict[symbol -> DataFrame] of daily candles (columns: time, open, high, low, close, volume), ascending.
- score_fn   : callable(df, asof_ts) -> float | None. The point-in-time score (e.g. the indicator rule_score),
               computed using ONLY rows with time <= asof_ts (the caller guarantees no look-ahead).
- records    : list of flat dicts {date, symbol, score, ref_close, fwd_<h>...} - one per (symbol, rebalance date).
- IC         : Information Coefficient - the Spearman rank correlation between the cross-section of scores on a
               date and their forward returns. Averaged over dates. IC ~ 0 means the score has no edge; a stable
               positive IC (t-stat > ~2) means higher score -> higher forward return.
"""
from __future__ import annotations

import bisect
from datetime import date
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

ScoreFn = Callable[[pd.DataFrame, pd.Timestamp], float | None]


# --------------------------------------------------------------------------- rank correlation
def _rankdata(a: np.ndarray) -> np.ndarray:
    """Average ranks (ties share the mean rank) - the ranking used by Spearman."""
    order = a.argsort(kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    ranks[order] = np.arange(1, len(a) + 1, dtype=float)
    # resolve ties to the average rank
    _, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    return (sums / counts)[inv]


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman rank correlation in [-1, 1]. None if fewer than 3 pairs or no variance (degenerate)."""
    xa, ya = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    mask = ~(np.isnan(xa) | np.isnan(ya))
    xa, ya = xa[mask], ya[mask]
    if len(xa) < 3:
        return None
    rx, ry = _rankdata(xa), _rankdata(ya)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


# --------------------------------------------------------------------------- record construction
def _loc_asof(times: list[pd.Timestamp], asof: pd.Timestamp) -> int | None:
    """Index of the last candle with time <= asof, or None if the series starts after asof."""
    i = bisect.bisect_right(times, asof) - 1
    return i if i >= 0 else None


def _as_date(ts: Any) -> date:
    return pd.Timestamp(ts).date()


def build_records(panel: dict[str, pd.DataFrame], score_fn: ScoreFn,
                  rebalance_dates: Sequence[Any], horizons: Sequence[int]) -> list[dict[str, Any]]:
    """One record per (symbol, rebalance date) with the point-in-time score and its forward returns.

    Forward return for horizon h = close[t+h] / close[t] - 1, using that symbol's own candles (h trading days
    ahead). A record whose score is None (insufficient history) is skipped; a horizon that runs past the data
    end is left None (counted as missing coverage, never as zero)."""
    records: list[dict[str, Any]] = []
    rbs = [pd.Timestamp(d) for d in rebalance_dates]
    for sym, df in panel.items():
        d = df.sort_values("time").reset_index(drop=True)
        times = list(d["time"])
        closes = d["close"].to_numpy(dtype=float)
        for rb in rbs:
            idx = _loc_asof(times, rb)
            if idx is None:
                continue
            score = score_fn(d, times[idx])
            if score is None:
                continue
            rec: dict[str, Any] = {"date": _as_date(times[idx]), "symbol": sym,
                                   "score": float(score), "ref_close": float(closes[idx])}
            for h in horizons:
                j = idx + h
                rec[f"fwd_{h}"] = float(closes[j] / closes[idx] - 1.0) if j < len(closes) else None
            records.append(rec)
    return records


# --------------------------------------------------------------------------- metrics
def _by_date(records: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    out: dict[date, list[dict[str, Any]]] = {}
    for r in records:
        out.setdefault(r["date"], []).append(r)
    return out


def cross_sectional_ic(records: list[dict[str, Any]], horizon: int, min_names: int = 5) -> dict[str, Any]:
    """Average Spearman IC between score and fwd_<horizon> across dates that have >= min_names ranked picks.

    Returns mean/std/t-stat and the share of dates with positive IC. The t-stat (mean / (std/sqrt(n))) is the
    headline: |t| > ~2 means the edge is unlikely to be noise."""
    key = f"fwd_{horizon}"
    ics: list[float] = []
    for _d, rows in _by_date(records).items():
        pairs = [(r["score"], r[key]) for r in rows if r.get(key) is not None]
        if len(pairs) < min_names:
            continue
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            ics.append(ic)
    if not ics:
        return {"n_cross_sections": 0, "ic_mean": None, "ic_std": None, "ic_t": None, "ic_hit_rate": None}
    arr = np.asarray(ics)
    mean, std = float(arr.mean()), float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    t = float(mean / (std / np.sqrt(len(arr)))) if std > 0 else None
    return {"n_cross_sections": len(ics), "ic_mean": round(mean, 4), "ic_std": round(std, 4),
            "ic_t": round(t, 2) if t is not None else None,
            "ic_hit_rate": round(float((arr > 0).mean()), 3)}


def bucket_returns(records: list[dict[str, Any]], horizon: int, n_buckets: int = 5,
                   min_names: int | None = None) -> dict[str, Any]:
    """Rank picks into `n_buckets` per date by score, then average the forward return in each bucket across all
    dates. Bucket 1 = lowest score, bucket N = highest. `long_short` = top bucket mean - bottom bucket mean: the
    return of a market-neutral book that is long the best-scored and short the worst-scored names."""
    key = f"fwd_{horizon}"
    min_names = min_names or n_buckets
    collected: dict[int, list[float]] = {b: [] for b in range(1, n_buckets + 1)}
    for _d, rows in _by_date(records).items():
        pairs = [(r["score"], r[key]) for r in rows if r.get(key) is not None]
        if len(pairs) < min_names:
            continue
        scores = np.asarray([p[0] for p in pairs])
        rets = np.asarray([p[1] for p in pairs])
        ranks = _rankdata(scores)
        labels = np.ceil(ranks / len(ranks) * n_buckets).astype(int).clip(1, n_buckets)
        for b in range(1, n_buckets + 1):
            vals = rets[labels == b]
            if len(vals):
                collected[b].append(float(vals.mean()))
    buckets = [{"bucket": b, "mean_fwd": round(float(np.mean(v)), 4) if v else None, "n_dates": len(v)}
               for b, v in collected.items()]
    # long-short = highest populated bucket - lowest populated bucket (robust to tied scores leaving a fixed
    # extreme bucket empty; with well-dispersed scores these are simply bucket N and bucket 1).
    populated = [b for b in range(1, n_buckets + 1) if collected[b]]
    long_short = (round(float(np.mean(collected[populated[-1]]) - np.mean(collected[populated[0]])), 4)
                  if len(populated) >= 2 else None)
    return {"n_buckets": n_buckets, "buckets": buckets, "long_short": long_short}


def sign_conditional(records: list[dict[str, Any]], horizon: int, threshold: float = 0.0) -> dict[str, Any]:
    """Pooled time-series view (works even for a single symbol): mean forward return when score > threshold vs
    score <= threshold, their spread, and the hit rate (share of score>threshold picks with a positive return)."""
    key = f"fwd_{horizon}"
    pos = [r[key] for r in records if r.get(key) is not None and r["score"] > threshold]
    neg = [r[key] for r in records if r.get(key) is not None and r["score"] <= threshold]
    hit = round(float(np.mean([p > 0 for p in pos])), 3) if pos else None
    pos_m = round(float(np.mean(pos)), 4) if pos else None
    neg_m = round(float(np.mean(neg)), 4) if neg else None
    spread = round(pos_m - neg_m, 4) if pos_m is not None and neg_m is not None else None
    return {"threshold": threshold, "pos_n": len(pos), "neg_n": len(neg),
            "pos_mean_fwd": pos_m, "neg_mean_fwd": neg_m, "spread": spread, "pos_hit_rate": hit}


def pooled_spearman(records: list[dict[str, Any]], horizon: int) -> float | None:
    """Spearman over the whole pool (all symbol-dates at once) - a coarse single number across everything."""
    key = f"fwd_{horizon}"
    pairs = [(r["score"], r[key]) for r in records if r.get(key) is not None]
    if len(pairs) < 3:
        return None
    ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
    return round(ic, 4) if ic is not None else None


def summarize(records: list[dict[str, Any]], horizons: Sequence[int], n_buckets: int = 5,
              min_names: int = 5) -> dict[str, Any]:
    """Full report over all horizons: coverage, cross-sectional IC, buckets + long-short, sign split, pooled rho."""
    dates = sorted({r["date"] for r in records})
    symbols = sorted({r["symbol"] for r in records})
    per_h: dict[str, Any] = {}
    for h in horizons:
        key = f"fwd_{h}"
        coverage = sum(1 for r in records if r.get(key) is not None)
        per_h[str(h)] = {
            "coverage": coverage,
            "ic": cross_sectional_ic(records, h, min_names=min_names),
            "buckets": bucket_returns(records, h, n_buckets=n_buckets, min_names=max(n_buckets, min_names)),
            "sign": sign_conditional(records, h),
            "pooled_spearman": pooled_spearman(records, h),
        }
    return {
        "n_records": len(records), "n_symbols": len(symbols), "n_dates": len(dates),
        "symbols": symbols, "date_range": [str(dates[0]), str(dates[-1])] if dates else [None, None],
        "horizons": [int(h) for h in horizons], "n_buckets": n_buckets, "min_names": min_names,
        "cross_sectional": len(symbols) >= min_names, "by_horizon": per_h,
    }
