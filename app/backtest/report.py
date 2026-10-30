"""Render the backtest summary to Markdown and write the per-pick CSV."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _num(x: float | None, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def to_markdown(summary: dict[str, Any], title: str = "Backtest - deterministic indicator rule_score") -> str:
    L: list[str] = []
    L.append(f"# {title}")
    L.append("")
    L.append(f"- Universe: **{summary['n_symbols']}** symbols, **{summary['n_dates']}** rebalance dates, "
             f"**{summary['n_records']}** scored picks")
    dr = summary["date_range"]
    L.append(f"- Period: **{dr[0]} -> {dr[1]}**")
    mode = "cross-sectional (IC / buckets / long-short valid)" if summary["cross_sectional"] else \
        f"single/thin universe (< {summary['min_names']} names: rely on the sign split; IC/buckets omitted)"
    L.append(f"- Mode: {mode}")
    L.append("")
    L.append("Forward return = close[t+h]/close[t] - 1. Score at t uses only candles up to t (no look-ahead). "
             "IC = mean Spearman rank corr (score vs forward return) across dates; |t| > ~2 ~ a real edge.")
    L.append("")

    for h in summary["horizons"]:
        blk = summary["by_horizon"][str(h)]
        ic, sign, bk = blk["ic"], blk["sign"], blk["buckets"]
        L.append(f"## Horizon: {h} trading days")
        L.append(f"- Coverage: {blk['coverage']} picks with a realised {h}-day return")
        L.append(f"- Pooled Spearman (all picks): **{_num(blk['pooled_spearman'])}**")
        if summary["cross_sectional"]:
            L.append(f"- Cross-sectional IC: mean **{_num(ic['ic_mean'])}**, std {_num(ic['ic_std'])}, "
                     f"t-stat **{_num(ic['ic_t'], 2)}**, IC>0 on {_num(ic['ic_hit_rate'], 3)} of "
                     f"{ic['n_cross_sections']} dates")
            L.append(f"- Long-short (top bucket - bottom bucket), {h}-day: **{_pct(bk['long_short'])}**")
            L.append("")
            L.append("| Score bucket | Mean fwd return | Dates |")
            L.append("|---|---|---|")
            for b in bk["buckets"]:
                tag = " (lowest)" if b["bucket"] == 1 else " (highest)" if b["bucket"] == bk["n_buckets"] else ""
                L.append(f"| {b['bucket']}{tag} | {_pct(b['mean_fwd'])} | {b['n_dates']} |")
        L.append("")
        L.append(f"- Sign split (score>0 vs <=0): long mean **{_pct(sign['pos_mean_fwd'])}** (n={sign['pos_n']}, "
                 f"hit {_num(sign['pos_hit_rate'], 3)}) vs **{_pct(sign['neg_mean_fwd'])}** (n={sign['neg_n']}); "
                 f"spread **{_pct(sign['spread'])}**")
        L.append("")

    L.append("---")
    L.append("_Validates the deterministic backbone only (daily indicator rule_score). The intraday MTF bonus "
             "and the LLM's +/-2 adjustment are not replayed. Yahoo adjusted daily candles; live scoring uses "
             "FYERS. Not investment advice._")
    return "\n".join(L)


def write_csv(records: list[dict[str, Any]], horizons: list[int], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["date", "symbol", "score", "ref_close"] + [f"fwd_{h}" for h in horizons]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(records, key=lambda x: (x["date"], x["symbol"])):
            w.writerow({c: r.get(c) for c in cols})
