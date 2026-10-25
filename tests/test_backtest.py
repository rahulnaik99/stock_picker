"""Deterministic unit tests for the backtest harness - synthetic price panels only, no network."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest import engine
from app.backtest.scores import indicator_score_asof


# --------------------------------------------------------------------------- synthetic data
def _trend(drift: float, n: int = 300, start: str = "2022-01-03") -> pd.DataFrame:
    """A clean daily candle series with constant geometric drift (no noise) - deterministic."""
    t = pd.date_range(start, periods=n, freq="B")
    close = 100.0 * np.power(1.0 + drift, np.arange(n))
    openp = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(openp, close) * 1.001
    low = np.minimum(openp, close) * 0.999
    return pd.DataFrame({"time": t, "open": openp, "high": high, "low": low,
                         "close": close, "volume": np.full(n, 1000.0)})


# --------------------------------------------------------------------------- rank correlation
def test_spearman_known_values():
    assert engine.spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)
    assert engine.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    # monotonic but non-linear -> Spearman still 1.0 (rank based)
    assert engine.spearman([1, 2, 3, 4], [1, 4, 9, 16]) == pytest.approx(1.0)
    assert engine.spearman([1, 2], [1, 2]) is None            # < 3 pairs
    assert engine.spearman([1, 1, 1], [1, 2, 3]) is None      # no variance


def test_rankdata_handles_ties():
    r = engine._rankdata(np.array([10.0, 20.0, 20.0, 40.0]))
    assert list(r) == [1.0, 2.5, 2.5, 4.0]


# --------------------------------------------------------------------------- record construction
def test_build_records_forward_returns_and_asof():
    close = np.arange(100, 130, dtype=float)                   # 100,101,...,129
    t = pd.date_range("2022-01-03", periods=len(close), freq="B")
    df = pd.DataFrame({"time": t, "open": close, "high": close, "low": close,
                       "close": close, "volume": 1.0})
    panel = {"X": df}
    asof = t[10]
    recs = engine.build_records(panel, lambda d, ts: 1.0, [asof], horizons=[2, 5])
    assert len(recs) == 1
    r = recs[0]
    assert r["symbol"] == "X" and r["ref_close"] == 110.0
    assert r["fwd_2"] == 112.0 / 110.0 - 1.0
    assert r["fwd_5"] == 115.0 / 110.0 - 1.0


def test_build_records_skips_none_score_and_truncated_horizon():
    close = np.arange(100, 110, dtype=float)
    t = pd.date_range("2022-01-03", periods=len(close), freq="B")
    df = pd.DataFrame({"time": t, "open": close, "high": close, "low": close, "close": close, "volume": 1.0})
    # score None -> skipped
    assert engine.build_records({"X": df}, lambda d, ts: None, [t[2]], [1]) == []
    # horizon runs past the data end -> fwd is None (not zero)
    recs = engine.build_records({"X": df}, lambda d, ts: 1.0, [t[-1]], [3])
    assert recs[0]["fwd_3"] is None


# --------------------------------------------------------------------------- metrics
def test_bucket_returns_monotonic():
    recs = [{"date": "d", "symbol": f"S{i}", "score": float(i), "fwd_1": float(i)} for i in range(1, 11)]
    bk = engine.bucket_returns(recs, horizon=1, n_buckets=5)
    means = [b["mean_fwd"] for b in bk["buckets"]]
    assert means == sorted(means)                              # higher score bucket -> higher fwd return
    assert bk["long_short"] > 0


def test_sign_conditional_split():
    recs = [{"date": "d", "symbol": "A", "score": 2.0, "fwd_1": 0.05},
            {"date": "d", "symbol": "B", "score": 1.0, "fwd_1": 0.01},
            {"date": "d", "symbol": "C", "score": -1.0, "fwd_1": -0.02},
            {"date": "d", "symbol": "D", "score": -3.0, "fwd_1": 0.01}]
    s = engine.sign_conditional(recs, horizon=1)
    assert s["pos_n"] == 2 and s["neg_n"] == 2
    assert s["pos_mean_fwd"] == 0.03 and s["pos_hit_rate"] == 1.0
    assert s["spread"] == round(0.03 - (-0.005), 4)


def test_cross_sectional_ic_positive_when_rank_aligned():
    recs = [{"date": "d", "symbol": f"S{i}", "score": float(i), "fwd_21": float(i) / 100} for i in range(1, 9)]
    ic = engine.cross_sectional_ic(recs, horizon=21, min_names=5)
    assert ic["n_cross_sections"] == 1 and ic["ic_mean"] == 1.0


# --------------------------------------------------------------------------- point-in-time score
def test_indicator_score_sign_matches_trend():
    up = _trend(0.01)
    down = _trend(-0.01)
    asof = up["time"].iloc[-1]
    assert indicator_score_asof(up, asof) > 0
    assert indicator_score_asof(down, down["time"].iloc[-1]) < 0
    # not enough history -> None
    assert indicator_score_asof(up.head(30), up["time"].iloc[29]) is None


def test_no_lookahead_score_uses_only_past():
    """The score at t must be identical whether or not future candles exist in the frame."""
    full = _trend(0.01, n=300)
    asof = full["time"].iloc[200]
    truncated = full[full["time"] <= asof]
    assert indicator_score_asof(full, asof) == indicator_score_asof(truncated, asof)


# --------------------------------------------------------------------------- end-to-end
def test_summarize_positive_edge_on_trending_panel():
    drifts = [-0.02, -0.012, -0.006, -0.002, 0.002, 0.006, 0.012, 0.02]
    panel = {f"S{i}": _trend(d) for i, d in enumerate(drifts)}
    dates = [panel["S0"]["time"].iloc[i] for i in range(120, 240, 20)]   # several rebalances, room for fwd
    recs = engine.build_records(panel, indicator_score_asof, dates, horizons=[5, 21])
    summary = engine.summarize(recs, horizons=[5, 21], n_buckets=4, min_names=5)
    assert summary["n_symbols"] == 8 and summary["cross_sectional"] is True
    # higher-drift names score higher AND earn more -> clearly positive rank correlation
    assert summary["by_horizon"]["21"]["pooled_spearman"] > 0.5
    assert summary["by_horizon"]["21"]["ic"]["ic_mean"] > 0.5
    assert summary["by_horizon"]["21"]["sign"]["spread"] > 0
