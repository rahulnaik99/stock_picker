"""CLI: replay the deterministic indicator score over history and measure its predictive power.

Example
-------
    python -m app.backtest \\
        --symbols CANBK,SBIN,PNB,BANKBARODA,UNIONBANK,INDIANB,CANFINHOME,BAJFINANCE \\
        --start 2022-01-01 --end 2026-09-30 \\
        --rebalance W --horizons 5,21,63 --out output/backtest

Writes <out>.md (summary) and <out>.csv (every scored pick + its forward returns), and prints the summary.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.backtest import data as btdata
from app.backtest import report as btreport
from app.backtest.engine import build_records, summarize
from app.backtest.scores import indicator_score_asof


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.backtest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbols", default="", help="comma-separated NSE symbols. Default: a ~28-name mixed-financials basket")
    p.add_argument("--start", required=True, help="history start (YYYY-MM-DD)")
    p.add_argument("--end", required=True, help="history end (YYYY-MM-DD)")
    p.add_argument("--rebalance", default="W", help="rebalance frequency: D, W, ME, Q (pandas freq). Default W")
    p.add_argument("--horizons", default="5,21,63", help="forward horizons in trading days. Default 5,21,63")
    p.add_argument("--buckets", type=int, default=5, help="number of score buckets for the cross-section. Default 5")
    p.add_argument("--min-names", type=int, default=5, help="min symbols per date for cross-sectional IC. Default 5")
    p.add_argument("--trades", action="store_true", help="also run the realistic Trader trade/portfolio backtest")
    p.add_argument("--capital", type=float, default=100000.0, help="starting capital for the portfolio sim")
    p.add_argument("--risk-pct", type=float, default=1.0, help="risk %% of CURRENT equity per trade")
    p.add_argument("--entry-mode", choices=["market", "limit"], default="market", help="trade entry fill model")
    p.add_argument("--hold-cap", type=int, default=63, help="max bars to hold a simulated trade before closing at market")
    p.add_argument("--no-short", action="store_true", help="long-only trade backtest")
    p.add_argument("--min-rr", type=float, default=0.0, help="skip trade setups whose reward:risk is below this")
    p.add_argument("--target-r", type=float, default=0.0, help="fixed take-profit at N*risk instead of the opposite zone")
    p.add_argument("--trail-r", type=float, default=0.0, help="trail the stop N*risk behind the best price (let winners run)")
    p.add_argument("--benchmark", default="", help="buy&hold benchmark symbol; default = equal-weight basket of the universe")
    p.add_argument("--out", default="output/backtest", help="output path prefix (writes .md and .csv)")
    a = p.parse_args(argv)

    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] or list(btdata.DEFAULT_UNIVERSE)
    horizons = [int(h) for h in a.horizons.split(",") if h.strip()]

    print(f"Loading daily candles for {len(symbols)} symbols {a.start}..{a.end} from Yahoo ...")
    panel = btdata.load_yahoo_daily(symbols, a.start, a.end)
    if not panel:
        print("No price data returned. Check symbols / dates / network.")
        return 2
    missing = [s for s in symbols if s not in panel]
    if missing:
        print(f"  (no data for: {', '.join(missing)})")
    dates = btdata.rebalance_dates(panel, a.rebalance)
    print(f"Scoring {len(panel)} symbols over {len(dates)} rebalance dates ({a.rebalance}) ...")

    records = build_records(panel, indicator_score_asof, dates, horizons)
    if not records:
        print("No scored picks (history too short for the indicator window). Widen the date range.")
        return 2
    summary = summarize(records, horizons, n_buckets=a.buckets, min_names=a.min_names)

    md = btreport.to_markdown(summary)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    md_path, csv_path, json_path = out.with_suffix(".md"), out.with_suffix(".csv"), out.with_suffix(".json")
    md_path.write_text(md)
    btreport.write_csv(records, horizons, csv_path)
    json_path.write_text(json.dumps(summary, indent=2))
    print("\n" + md + "\n")
    print(f"Wrote: {md_path}  |  {csv_path}  |  {json_path}")

    if a.trades:
        from app.backtest import trades as bttrades

        trs = bttrades.run_trade_portfolio(panel, dates, capital=a.capital, risk_pct=a.risk_pct, max_bars=a.hold_cap,
                                           entry_mode=a.entry_mode, allow_short=not a.no_short,
                                           min_rr=a.min_rr, target_r=a.target_r, trail_r=a.trail_r)
        tsum = bttrades.summarize_portfolio(trs, a.capital)
        bench = bttrades.benchmark_buy_and_hold(panel, a.capital, a.benchmark or None)
        knobs = {"min_rr": a.min_rr, "target_r": a.target_r, "trail_r": a.trail_r}
        tmd = bttrades.portfolio_to_markdown(tsum, a.risk_pct, a.entry_mode, knobs=knobs, benchmark=bench)
        tmd_path = out.with_name(out.stem + "_trades").with_suffix(".md")
        tcsv_path = out.with_name(out.stem + "_trades").with_suffix(".csv")
        eq_path = out.with_name(out.stem + "_equity").with_suffix(".csv")
        tmd_path.write_text(tmd)
        _write_trades_csv(trs, tcsv_path)
        _write_equity_csv(bttrades.equity_curve_rows(trs, a.capital), bench, eq_path)
        print("\n" + tmd + "\n")
        print(f"Wrote: {tmd_path}  |  {tcsv_path}  |  {eq_path}")
    return 0


def _write_trades_csv(recs: list[dict], path: Path) -> None:
    import csv
    cols = ["entry_date", "exit_date", "symbol", "bias", "entry", "stop_loss", "target", "quantity", "notional",
            "risk_reward", "outcome", "exit_price", "bars_held", "pnl_per_share", "pnl", "r_multiple"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(recs, key=lambda x: (x["entry_date"], x["symbol"])):
            w.writerow({c: (r.get(c).date() if hasattr(r.get(c), "date") else r.get(c)) for c in cols})


def _write_equity_csv(curve: list, bench: dict | None, path: Path) -> None:
    """strategy equity (by trade-exit date) + the benchmark equity curve, each as date,label,equity rows."""
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["series", "date", "equity"])
        for dt, eq in curve:
            if dt is not None:
                w.writerow(["strategy", dt.date() if hasattr(dt, "date") else dt, eq])
        if bench:
            for dt, eq in bench.get("curve", []):
                w.writerow([f"benchmark:{bench['label']}", dt, eq])


if __name__ == "__main__":
    raise SystemExit(main())
