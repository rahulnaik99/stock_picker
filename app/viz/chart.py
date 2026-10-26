"""Technical chart rendering for the Trader lens.

`plot_trade_chart(daily, zones, plan, path, title, lookback, ax, ax_vol)`
    Single chart — standalone (path set, creates own figure with volume) or composited (ax/ax_vol supplied).

`plot_dual_chart(daily, zones, long_plan, short_plan, path, symbol, capital, risk_pct, lookback)`
    Side-by-side dual chart: LEFT = LONG setup, RIGHT = SHORT setup. Both panels share the same zone
    layout; plan lines differ. Volume sub-panel below each price panel. MTF alignment badge stamped on each.
"""
from __future__ import annotations

from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt         # noqa: E402
import matplotlib.gridspec as gridspec  # noqa: E402
import pandas as pd                     # noqa: E402

_BG, _GRID, _PRICE = "#ffffff", "#e6e6e6", "#111111"
_EMA = {"ema20": "#e8820c", "ema50": "#1f77b4", "ema200": "#7a3fb0"}
_DEMAND, _SUPPLY, _VWAP = "#1a9850", "#d6301d", "#888888"
_VOL_UP, _VOL_DN = "#1a9850", "#d6301d"
_LONG_E, _LONG_S, _LONG_T = "#1a9850", "#d6301d", "#1f77b4"
_SHORT_E, _SHORT_S, _SHORT_T = "#d6301d", "#1a9850", "#1f77b4"
_EVENT_COLORS = {"results": "#6A0DAD", "dividend": "#1a9850", "board_meeting": "#e8820c",
                 "fundraise": "#1f77b4", "analyst_meet": "#555", "merger_acquisition": "#d6301d"}


# ──────────────────────────────────────────────────── internal drawing helpers ────

def _draw_events(ax: plt.Axes, daily: pd.DataFrame, lookback: int,
                 events: list[dict[str, Any]] | None) -> None:
    """Mark past events as vertical dashed lines; collect upcoming events for an annotation box."""
    if not events:
        return
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    if d.empty:
        return
    chart_start = d["time"].iloc[0]
    chart_end = d["time"].iloc[-1]
    upcoming_lines: list[str] = []
    for ev in events:
        edate_str = ev.get("date") or ""
        if not edate_str:
            continue
        try:
            edate = pd.Timestamp(edate_str)
        except (ValueError, TypeError):
            continue
        etype = ev.get("type", "other")
        col = _EVENT_COLORS.get(etype, "#555")
        label = f"{etype.replace('_',' ').title()} {edate.strftime('%d%b')}"
        if chart_start <= edate <= chart_end:
            ax.axvline(edate, color=col, linestyle=":", linewidth=1.1, alpha=0.85, zorder=4)
            ax.text(edate, ax.get_ylim()[1] if ax.get_ylim()[1] != 1.0 else 1,
                    f"  {label}", color=col, fontsize=6.5, rotation=90, va="top", clip_on=True)
        elif edate > chart_end:
            upcoming_lines.append(f"  {label}")
    if upcoming_lines:
        ax.text(0.01, 0.03, "Upcoming:\n" + "\n".join(upcoming_lines[:5]),
                transform=ax.transAxes, fontsize=7, va="bottom", ha="left",
                color="#334155", bbox=dict(facecolor="white", alpha=0.82, edgecolor=_GRID, boxstyle="round,pad=0.3"))


def _draw_base(ax: plt.Axes, daily: pd.DataFrame, zones: dict[str, Any], lookback: int) -> None:
    """Draw price, EMAs, demand/supply zone bands, VWAP — everything except plan-specific lines."""
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    x = pd.to_datetime(d["time"])
    ax.set_facecolor(_BG)
    ax.grid(True, color=_GRID, linewidth=0.5, zorder=0)
    ax.vlines(x, d["low"], d["high"], color="#adb5bd", linewidth=0.5, zorder=1)
    ax.plot(x, d["close"], color=_PRICE, linewidth=1.5, label="Close", zorder=3)
    for key, col in _EMA.items():
        span = int(key[3:])
        if len(d) >= span:
            ax.plot(x, d["close"].ewm(span=span, adjust=False).mean(),
                    color=col, linewidth=0.9, alpha=0.9, label=key.upper(), zorder=2)
    xmax = x.iloc[-1]
    nd, ns = zones.get("nearest_demand"), zones.get("nearest_supply")
    for z in (zones.get("demand_zones") or []):
        near = nd and abs(z["mid"] - nd["mid"]) < 0.01
        ax.axhspan(z["low"], z["high"], color=_DEMAND, alpha=0.32 if near else 0.11, zorder=0)
    if nd:
        ax.text(xmax, nd["high"], f"  D {nd['low']}–{nd['high']} s{nd.get('strength','?')}",
                color=_DEMAND, fontsize=7, va="bottom", ha="left", clip_on=True)
    for z in (zones.get("supply_zones") or []):
        near = ns and abs(z["mid"] - ns["mid"]) < 0.01
        ax.axhspan(z["low"], z["high"], color=_SUPPLY, alpha=0.32 if near else 0.11, zorder=0)
    if ns:
        ax.text(xmax, ns["low"], f"  S {ns['low']}–{ns['high']} s{ns.get('strength','?')}",
                color=_SUPPLY, fontsize=7, va="top", ha="left", clip_on=True)
    if zones.get("vwap_session") is not None:
        ax.axhline(zones["vwap_session"], color=_VWAP, linestyle=":", linewidth=1.0, label="VWAP", zorder=2)


def _draw_volume(ax_vol: plt.Axes, daily: pd.DataFrame, lookback: int) -> None:
    """Volume bars (green=up day, red=down day) + 20-bar rolling average."""
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    x = pd.to_datetime(d["time"])
    colors = [_VOL_UP if c >= o else _VOL_DN for c, o in zip(d["close"], d["open"])]
    ax_vol.bar(x, d["volume"], color=colors, alpha=0.65, width=0.7, zorder=2)
    vol_ma = d["volume"].rolling(20, min_periods=1).mean()
    ax_vol.plot(x, vol_ma, color="#555", linewidth=0.9, label="Vol MA20", zorder=3)
    ax_vol.set_facecolor(_BG)
    ax_vol.grid(True, color=_GRID, linewidth=0.4, axis="y", zorder=0)
    ax_vol.set_ylabel("Volume", fontsize=7, labelpad=2)
    ax_vol.yaxis.set_major_formatter(plt.FuncFormatter(
        lambda v, _: f"{v/1e7:.1f}Cr" if v >= 1e7 else (f"{v/1e6:.1f}M" if v >= 1e6 else f"{v/1e3:.0f}K")))
    ax_vol.legend(fontsize=6, loc="upper left")


def _draw_plan(ax: plt.Axes, daily: pd.DataFrame, plan: dict[str, Any], lookback: int) -> None:
    bias = (plan or {}).get("bias")
    if bias not in ("long", "short"):
        ax.text(0.5, 0.5, "No directional setup\n(mid-range or missing zone)",
                transform=ax.transAxes, ha="center", va="center", fontsize=10, color="#aaa",
                bbox=dict(facecolor="white", alpha=0.8, boxstyle="round"))
        return
    e_col, s_col, t_col = (_LONG_E, _LONG_S, _LONG_T) if bias == "long" else (_SHORT_E, _SHORT_S, _SHORT_T)
    d = daily.sort_values("time").tail(lookback)
    xmin = pd.to_datetime(d["time"].iloc[0])
    for lvl, col, name, ls in [(plan.get("entry"), e_col, "entry", "-"),
                                (plan.get("stop_loss"), s_col, "SL", "--"),
                                (plan.get("target"), t_col, "target", "--")]:
        if lvl is not None:
            ax.axhline(lvl, color=col, linestyle=ls, linewidth=1.4 if name == "entry" else 1.0, zorder=5)
            ax.text(xmin, lvl, f" {name} {lvl}", color=col, fontsize=8, va="center",
                    ha="left", weight="bold" if name == "entry" else "normal")
    rr, qty = plan.get("risk_reward"), plan.get("quantity")
    ax.set_xlabel(f"{bias.upper()}  entry {plan.get('entry')}  SL {plan.get('stop_loss')}  "
                  f"target {plan.get('target')}  R:R {rr}  qty {qty}", fontsize=7.5, labelpad=4, color="#334155")


def _mtf_badge(ax: plt.Axes, zones: dict[str, Any]) -> None:
    mtf = zones.get("mtf") or {}
    align = zones.get("mtf_bias") or mtf.get("alignment") or "—"
    bias_map = mtf.get("bias") or {}
    vc = zones.get("volume_context") or {}
    vol_line = f"  Vol: {vc.get('trend','?')} ({vc.get('ratio','?')}×)" if vc else ""
    lines = [f"MTF: {align}"]
    for tf, label in [("1d", "D"), ("1h", "1h"), ("15m", "15m")]:
        if tf in bias_map:
            lines.append(f"  {label}: {bias_map[tf]}")
    if vol_line:
        lines.append(vol_line)
    ax.text(0.985, 0.985, "\n".join(lines), transform=ax.transAxes, fontsize=7.5,
            va="top", ha="right", family="monospace",
            bbox=dict(facecolor="white", alpha=0.88, edgecolor=_GRID, boxstyle="round,pad=0.3"))


# ──────────────────────────────────────────────────── public API ────

def plot_trade_chart(daily: pd.DataFrame, zones: dict[str, Any], trade_plan: dict[str, Any] | None,
                     path: str | None, title: str | None = None, lookback: int = 160,
                     ax: plt.Axes | None = None, ax_vol: plt.Axes | None = None,
                     events: list[dict[str, Any]] | None = None) -> str | None:
    """Single chart. With no ax/ax_vol, creates its own figure (price + volume), saves to `path` and returns it.
    When ax is supplied, draws on that axis only (used by plot_dual_chart; no save)."""
    standalone = ax is None
    if standalone:
        fig = plt.figure(figsize=(14, 8), dpi=110)
        fig.patch.set_facecolor(_BG)
        gs_ = gridspec.GridSpec(2, 1, figure=fig, height_ratios=[4, 1], hspace=0.04)
        ax = fig.add_subplot(gs_[0])
        ax_vol = fig.add_subplot(gs_[1], sharex=ax)
    _draw_base(ax, daily, zones, lookback)
    _draw_plan(ax, daily, trade_plan or {}, lookback)
    _draw_events(ax, daily, lookback, events)
    _mtf_badge(ax, zones)
    if title:
        ax.set_title(title, fontsize=11, color=_PRICE, pad=6)
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.9)
    ax.set_ylabel("price (INR)", fontsize=9)
    plt.setp(ax.get_xticklabels(), visible=False)
    if ax_vol is not None:
        _draw_volume(ax_vol, daily, lookback)
    if standalone:
        if fig:
            fig.tight_layout()
            fig.savefig(path, facecolor=_BG)
            plt.close(fig)
    return path


def plot_dual_chart(daily: pd.DataFrame, zones: dict[str, Any],
                    long_plan: dict[str, Any], short_plan: dict[str, Any],
                    path: str, symbol: str = "", lookback: int = 160,
                    events: list[dict[str, Any]] | None = None) -> str:
    """Side-by-side dual chart: LEFT = LONG, RIGHT = SHORT. Each panel has a volume sub-panel below."""
    fig = plt.figure(figsize=(24, 10), dpi=110)
    fig.patch.set_facecolor(_BG)
    gs_ = gridspec.GridSpec(2, 2, figure=fig, height_ratios=[4, 1], hspace=0.04, wspace=0.22)
    ax_l = fig.add_subplot(gs_[0, 0])
    axv_l = fig.add_subplot(gs_[1, 0], sharex=ax_l)
    ax_r = fig.add_subplot(gs_[0, 1])
    axv_r = fig.add_subplot(gs_[1, 1], sharex=ax_r)

    stack = zones.get("ema_stack", "—")
    vwap_s = zones.get("vwap_side") or "—"
    vc = zones.get("volume_context") or {}
    vol_txt = f"  Vol {vc.get('trend','?')} {vc.get('ratio','?')}×" if vc else ""
    for ax, axv, plan, side, accent in [
        (ax_l, axv_l, long_plan,  f"LONG  — {symbol}  EMA: {stack}  VWAP: {vwap_s}{vol_txt}",  _DEMAND),
        (ax_r, axv_r, short_plan, f"SHORT — {symbol}  EMA: {stack}  VWAP: {vwap_s}{vol_txt}", _SUPPLY),
    ]:
        _draw_base(ax, daily, zones, lookback)
        _draw_plan(ax, daily, plan, lookback)
        _draw_events(ax, daily, lookback, events)
        _mtf_badge(ax, zones)
        ax.set_title(side, fontsize=11, color=accent, weight="bold", pad=7)
        ax.legend(loc="upper left", fontsize=7.5, framealpha=0.9)
        ax.set_ylabel("price (INR)", fontsize=9)
        plt.setp(ax.get_xticklabels(), visible=False)
        _draw_volume(axv, daily, lookback)

    import warnings
    fig.suptitle(f"Trader analysis — {symbol}   green = demand/support   red = supply/resistance   "
                 f"volume bars show conviction", fontsize=9.5, color="#334155", y=1.005)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fig.tight_layout(pad=1.5)
    fig.savefig(path, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    return path
