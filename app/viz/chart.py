"""Chart rendering for the Trader lens: price + EMAs + demand/supply zones + VWAP, with the bull/bear trade plan
(entry / stop / target) drawn on top. Matplotlib only; headless (Agg) so it runs anywhere.
"""
from __future__ import annotations

from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

# Neutral, light-mode trading palette (green = demand/long, red = supply/short - domain convention, not a brand).
_BG, _GRID, _PRICE = "#ffffff", "#e6e6e6", "#111111"
_EMA = {"ema20": "#e8820c", "ema50": "#1f77b4", "ema200": "#7a3fb0"}
_DEMAND, _SUPPLY, _VWAP = "#1a9850", "#d6301d", "#888888"
_ENTRY, _STOP, _TARGET = "#1f77b4", "#d6301d", "#1a9850"


def plot_trade_chart(daily: pd.DataFrame, zones: dict[str, Any], trade_plan: dict[str, Any] | None,
                     path: str, title: str | None = None, lookback: int = 160) -> str:
    """Render the last `lookback` daily candles with demand (green) / supply (red) zones, EMA20/50/200, session
    VWAP and - if a directional plan exists - the entry / stop-loss / target levels. Saves a PNG to `path`."""
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    x = pd.to_datetime(d["time"])
    fig, ax = plt.subplots(figsize=(13, 7), dpi=110)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    ax.grid(True, color=_GRID, linewidth=0.7, zorder=0)

    # OHLC wicks + close line (lightweight candlestick feel without extra deps)
    up = d["close"] >= d["open"]
    ax.vlines(x, d["low"], d["high"], color="#9aa0a6", linewidth=0.6, zorder=1)
    ax.plot(x, d["close"], color=_PRICE, linewidth=1.4, label="Close", zorder=3)
    for n, col in _EMA.items():
        v = zones.get(n)
        series = d["close"].ewm(span=int(n[3:]), adjust=False).mean()
        if v is not None:
            ax.plot(x, series, color=col, linewidth=1.0, alpha=0.9, label=n.upper(), zorder=2)

    xmin, xmax = x.iloc[0], x.iloc[-1]
    for z in (zones.get("demand_zones") or []):
        ax.axhspan(z["low"], z["high"], color=_DEMAND, alpha=0.13, zorder=0)
    for z in (zones.get("supply_zones") or []):
        ax.axhspan(z["low"], z["high"], color=_SUPPLY, alpha=0.13, zorder=0)
    nd, ns = zones.get("nearest_demand"), zones.get("nearest_supply")
    if nd:
        ax.axhspan(nd["low"], nd["high"], color=_DEMAND, alpha=0.28, zorder=1)
        ax.text(xmax, nd["high"], f"  demand {nd['low']}-{nd['high']} (str {nd.get('strength')})",
                color=_DEMAND, fontsize=8, va="bottom", ha="left")
    if ns:
        ax.axhspan(ns["low"], ns["high"], color=_SUPPLY, alpha=0.28, zorder=1)
        ax.text(xmax, ns["low"], f"  supply {ns['low']}-{ns['high']} (str {ns.get('strength')})",
                color=_SUPPLY, fontsize=8, va="top", ha="left")
    if zones.get("vwap_session") is not None:
        ax.axhline(zones["vwap_session"], color=_VWAP, linestyle=":", linewidth=1.0, label="VWAP", zorder=2)

    bias = (trade_plan or {}).get("bias")
    if bias in ("long", "short"):
        for lvl, col, name in ((trade_plan.get("entry"), _ENTRY, "entry"),
                               (trade_plan.get("stop_loss"), _STOP, "stop"),
                               (trade_plan.get("target"), _TARGET, "target")):
            if lvl is not None:
                ax.axhline(lvl, color=col, linestyle="--" if name != "entry" else "-", linewidth=1.3, zorder=4)
                ax.text(xmin, lvl, f"{name} {lvl} ", color=col, fontsize=8.5, va="center", ha="right", weight="bold")
        rr, qty = trade_plan.get("risk_reward"), trade_plan.get("quantity")
        sub = f"{bias.upper()}  entry {trade_plan.get('entry')} / SL {trade_plan.get('stop_loss')} / target {trade_plan.get('target')}  |  R:R {rr}, qty {qty}"
    else:
        sub = "no directional setup (mid-range / mixed)"

    ax.set_title(f"{title or 'Trader chart'}  —  {sub}", fontsize=11, color=_PRICE)
    ax.set_xlabel("date", fontsize=9)
    ax.set_ylabel("price (INR)", fontsize=9)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, facecolor=_BG)
    plt.close(fig)
    return path
