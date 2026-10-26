"""CLI: plot a stock's Trader chart (demand/supply zones + EMA/VWAP + bull/bear entry-stop-target).

Example:
    python -m app.viz --symbol CANBK --out output/CANBK.png
    python -m app.viz --symbol SBIN --start 2024-01-01 --end 2026-09-30 --out output/SBIN.png

Uses free Yahoo daily candles (no FYERS token). VWAP needs intraday and is omitted in this offline chart.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.viz", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", required=True, help="NSE symbol, e.g. CANBK")
    p.add_argument("--start", default="", help="history start YYYY-MM-DD (default ~2 years back)")
    p.add_argument("--end", default="", help="history end YYYY-MM-DD (default today)")
    p.add_argument("--capital", type=float, default=100000.0, help="capital for the plan's position size")
    p.add_argument("--risk-pct", type=float, default=1.0, help="risk %% of capital for the plan")
    p.add_argument("--lookback", type=int, default=160, help="bars to show")
    p.add_argument("--out", default="", help="output PNG path (default output/<symbol>.png)")
    a = p.parse_args(argv)

    start = a.start or (date.today() - timedelta(days=760)).isoformat()
    end = a.end or date.today().isoformat()
    sym = a.symbol.strip().upper()
    out = a.out or f"output/{sym}.png"

    from app.mcp_server.tools import indicators
    from app.viz.chart import plot_trade_chart

    # Try FYERS first (the live data source the app already uses); only fall back to Yahoo if the token
    # isn't set or has expired, so you can still run the chart offline without a fresh login.
    daily = None
    provider = None
    try:
        from app.mcp_server.tools.market import fetch_candles
        _df, provider = fetch_candles(sym, "D", 420, None)
        if len(_df) >= 60:
            daily = _df
            print(f"Loaded {len(daily)} bars from {provider}")
    except Exception as _exc:
        print(f"FYERS not available ({type(_exc).__name__}: {str(_exc)[:80]}), falling back to Yahoo ...")
    if daily is None:
        from app.backtest import data as btdata
        panel = btdata.load_yahoo_daily([sym], start, end)
        if sym not in panel or len(panel[sym]) < 60:
            print(f"Not enough data for {sym}. Ensure your FYERS token is set (`python -m scripts.fyers_login`) "
                  f"or check Yahoo connectivity / the date range.")
            return 2
        daily = panel[sym]
        provider = "yahoo"
    zones = indicators.demand_supply_zones(daily.tail(420), None)
    long_plan = indicators.build_trade_plan(zones, capital=a.capital, risk_pct=a.risk_pct, force_bias="long")
    short_plan = indicators.build_trade_plan(zones, capital=a.capital, risk_pct=a.risk_pct, force_bias="short")
    # Fetch upcoming events for chart markers (requires NSE session; skipped gracefully if unavailable).
    events: list[dict] = []
    try:
        from app.mcp_server.tools import nse as nse_tools
        nse_tools._get_session()          # warms the session quietly
        ev = nse_tools.nse_company_events(sym, days=90)
        if ev.get("status") == "ok":
            events = ev.get("data") or []
    except Exception:  # noqa: BLE001 - events are optional; don't fail the chart
        pass
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    from app.viz.chart import plot_dual_chart
    plot_dual_chart(daily, zones, long_plan, short_plan, out, symbol=sym, lookback=a.lookback, events=events)
    print(f"Wrote: {out}")
    print(f"LONG  plan: entry {long_plan.get('entry')} / SL {long_plan.get('stop_loss')} / target {long_plan.get('target')} | R:R {long_plan.get('risk_reward')} qty {long_plan.get('quantity')}")
    print(f"SHORT plan: entry {short_plan.get('entry')} / SL {short_plan.get('stop_loss')} / target {short_plan.get('target')} | R:R {short_plan.get('risk_reward')} qty {short_plan.get('quantity')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
