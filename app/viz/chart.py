"""Technical chart rendering — Trader lens.

All price panels use OHLC CANDLESTICKS (green body = up day/bar, red body = down).

`plot_quad_chart` — 4-panel layout:
  Row 0 (context):    Long-term 1D (365 bars)      |  15-min short-term (entry timing)
  Row 1 (plans):      LONG plan (green focus)       |  SHORT plan (red focus)
  Row 2 (volume):     Volume + MA20 (LONG)          |  Volume + MA20 (SHORT)

The 15-min y-axis is clamped to the intraday price range; plan levels beyond that range
are shown as edge-annotations so they never squish the visible candles.
"""
from __future__ import annotations

import warnings
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import matplotlib.dates as mdates       # noqa: E402
import matplotlib.gridspec as gridspec  # noqa: E402
import pandas as pd                     # noqa: E402
import numpy as np                      # noqa: E402

_BG, _GRID, _PRICE = "#ffffff", "#e6e6e6", "#111111"
_UP, _DN = "#1a9850", "#d6301d"        # candle body colours (green=up, red=down)
_EMA_COLORS = {"ema20": "#e8820c", "ema50": "#1f77b4", "ema200": "#7a3fb0"}
_DEMAND, _SUPPLY, _VWAP = "#1a9850", "#d6301d", "#888888"
_LONG_E, _LONG_S, _LONG_T = "#1a9850", "#d6301d", "#1f77b4"
_SHORT_E, _SHORT_S, _SHORT_T = "#d6301d", "#1a9850", "#1f77b4"
_EVENT_COLORS = {"results": "#6A0DAD", "dividend": "#1a9850", "board_meeting": "#e8820c",
                 "fundraise": "#1f77b4", "analyst_meet": "#555", "merger_acquisition": "#d6301d"}


# ──────────────────────────── candlestick primitive ─────────────────────────

def _candles(ax: plt.Axes, d: pd.DataFrame, width_frac: float = 0.65) -> None:
    """Draw OHLC candlesticks: colored body rectangle + wick lines. Handles any bar size."""
    if d.empty:
        return
    x = mdates.date2num(pd.to_datetime(d["time"]).dt.to_pydatetime())
    bw = (x[1] - x[0]) * width_frac if len(x) > 1 else 0.5
    up = d["close"].values >= d["open"].values
    colors = [_UP if u else _DN for u in up]
    lo = np.minimum(d["open"].values, d["close"].values)
    hi = np.maximum(d["open"].values, d["close"].values)
    # Wicks (behind bodies)
    ax.vlines(x, d["low"].values, d["high"].values, colors=colors, linewidth=0.6, zorder=2)
    # Bodies
    ax.bar(x, hi - lo, bottom=lo, width=bw, color=colors, alpha=0.88, zorder=3)
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))


# ──────────────────────────── shared draw helpers ───────────────────────────

def _draw_zones(ax: plt.Axes, zones: dict[str, Any], d_alpha: float = 0.32,
                s_alpha: float = 0.32, d_other: float = 0.11, s_other: float = 0.11) -> None:
    nd, ns = zones.get("nearest_demand"), zones.get("nearest_supply")
    for z in (zones.get("demand_zones") or []):
        near = nd and abs(z["mid"] - nd["mid"]) < 0.01
        ax.axhspan(z["low"], z["high"], color=_DEMAND, alpha=d_alpha if near else d_other, zorder=0)
    for z in (zones.get("supply_zones") or []):
        near = ns and abs(z["mid"] - ns["mid"]) < 0.01
        ax.axhspan(z["low"], z["high"], color=_SUPPLY, alpha=s_alpha if near else s_other, zorder=0)


def _draw_emas(ax: plt.Axes, d: pd.DataFrame, spans: list[int] | None = None) -> None:
    c = d["close"]
    for key, col in _EMA_COLORS.items():
        span = int(key[3:])
        if spans and span not in spans:
            continue
        if len(d) >= span:
            lw = 1.8 if span == 200 else 1.0
            ax.plot(mdates.date2num(pd.to_datetime(d["time"]).dt.to_pydatetime()),
                    c.ewm(span=span, adjust=False).mean().values,
                    color=col, linewidth=lw, alpha=0.9, label=key.upper(), zorder=4)


def _draw_events(ax: plt.Axes, daily: pd.DataFrame, lookback: int,
                 events: list[dict[str, Any]] | None) -> None:
    if not events:
        return
    d = daily.sort_values("time").tail(lookback)
    if d.empty:
        return
    chart_start, chart_end = d["time"].iloc[0], d["time"].iloc[-1]
    upcoming: list[str] = []
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
        label = f"{etype.replace('_', ' ').title()} {edate.strftime('%d%b')}"
        if chart_start <= edate <= chart_end:
            ax.axvline(mdates.date2num(edate.to_pydatetime()),
                       color=col, linestyle=":", linewidth=1.1, alpha=0.85, zorder=5)
        elif edate > chart_end:
            upcoming.append(f"  {label}")
    if upcoming:
        ax.text(0.01, 0.03, "Upcoming:\n" + "\n".join(upcoming[:5]),
                transform=ax.transAxes, fontsize=7, va="bottom", ha="left",
                bbox=dict(facecolor="white", alpha=0.82, edgecolor=_GRID, boxstyle="round,pad=0.3"))


def _mtf_badge(ax: plt.Axes, zones: dict[str, Any]) -> None:
    mtf = zones.get("mtf") or {}
    align = zones.get("mtf_bias") or mtf.get("alignment") or "—"
    bias_map = mtf.get("bias") or {}
    vc = zones.get("volume_context") or {}
    vol_line = f"  Vol: {vc.get('trend','?')} ({vc.get('ratio','?')}×)" if vc else ""
    lines = [f"MTF: {align}"] + [f"  {tf}: {bias_map[tf]}" for tf in ("1d", "1h", "15m") if tf in bias_map]
    if vol_line:
        lines.append(vol_line)
    ax.text(0.985, 0.985, "\n".join(lines), transform=ax.transAxes, fontsize=7.5,
            va="top", ha="right", family="monospace",
            bbox=dict(facecolor="white", alpha=0.88, edgecolor=_GRID, boxstyle="round,pad=0.3"))


def _draw_volume(ax_vol: plt.Axes, daily: pd.DataFrame, lookback: int) -> None:
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    x = mdates.date2num(pd.to_datetime(d["time"]).dt.to_pydatetime())
    colors = [_UP if c >= o else _DN for c, o in zip(d["close"], d["open"])]
    if len(x) > 1:
        bw = (x[1] - x[0]) * 0.7
    else:
        bw = 0.5
    ax_vol.bar(x, d["volume"].values, color=colors, width=bw, alpha=0.65, zorder=2)
    ax_vol.plot(x, d["volume"].rolling(20, min_periods=1).mean().values,
                color="#555", linewidth=0.9, label="Vol MA20", zorder=3)
    ax_vol.set_facecolor(_BG)
    ax_vol.grid(True, color=_GRID, linewidth=0.4, axis="y", zorder=0)
    ax_vol.set_ylabel("Volume", fontsize=7, labelpad=2)
    ax_vol.yaxis.set_major_formatter(plt.FuncFormatter(
        lambda v, _: f"{v/1e7:.1f}Cr" if v >= 1e7 else (f"{v/1e6:.1f}M" if v >= 1e6 else f"{v/1e3:.0f}K")))
    ax_vol.legend(fontsize=6, loc="upper left")
    ax_vol.xaxis_date()
    ax_vol.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax_vol.xaxis.get_major_locator()))


# ──────────────────────────── panel draw functions ──────────────────────────

def _draw_longterm(ax: plt.Axes, daily: pd.DataFrame, zones: dict[str, Any], lookback: int = 365,
                   events: list[dict[str, Any]] | None = None) -> None:
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    ax.set_facecolor("#fafffe")
    ax.grid(True, color=_GRID, linewidth=0.4, zorder=0)
    _candles(ax, d, width_frac=0.65)
    _draw_emas(ax, d)                     # all three EMAs; EMA200 is drawn thick
    nd, ns = zones.get("nearest_demand"), zones.get("nearest_supply")
    x_max = mdates.date2num(pd.to_datetime(d["time"].iloc[-1]).to_pydatetime())
    _draw_zones(ax, zones, d_alpha=0.18, s_alpha=0.18, d_other=0.07, s_other=0.07)
    if nd:
        ax.text(x_max, nd["high"], f"  D{nd['low']}–{nd['high']}",
                color=_DEMAND, fontsize=6.5, va="bottom", ha="left", clip_on=True)
    if ns:
        ax.text(x_max, ns["low"], f"  S{ns['low']}–{ns['high']}",
                color=_SUPPLY, fontsize=6.5, va="top", ha="left", clip_on=True)
    if zones.get("vwap_session") is not None:
        ax.axhline(zones["vwap_session"], color=_VWAP, linestyle=":", linewidth=1.0, label="VWAP", zorder=4)
    _draw_events(ax, daily, lookback, events)
    _mtf_badge(ax, zones)
    ax.set_title("Long Term — 1D  (big picture: EMA structure + key zones)", fontsize=10,
                 color="#2c5282", weight="bold", pad=5)
    ax.legend(loc="upper left", fontsize=7, framealpha=0.9)
    ax.set_ylabel("price (INR)", fontsize=8)


def _draw_15min(ax: plt.Axes, intraday: pd.DataFrame | None, long_plan: dict[str, Any],
                short_plan: dict[str, Any], lookback: int = 120) -> None:
    ax.set_facecolor("#f5f7ff")
    ax.grid(True, color=_GRID, linewidth=0.4, zorder=0)
    if intraday is None or len(intraday) < 10:
        ax.text(0.5, 0.5, "15-min data not available\n(FYERS required for intraday)",
                transform=ax.transAxes, ha="center", va="center", fontsize=10, color="#888",
                bbox=dict(facecolor="white", alpha=0.8, boxstyle="round"))
        ax.set_title("Short Term — 15min  (entry / exit timing)", fontsize=10,
                     color="#1a3a6b", weight="bold", pad=5)
        return
    d = intraday.sort_values("time").tail(lookback).reset_index(drop=True)
    _candles(ax, d, width_frac=0.70)
    _draw_emas(ax, d, spans=[9, 20])      # fast EMAs for intraday

    # Determine the y-range from the intraday candles themselves
    pad = (d["high"].max() - d["low"].min()) * 0.12
    y_lo = d["low"].min() - pad
    y_hi = d["high"].max() + pad
    ax.set_ylim(y_lo, y_hi)

    # Draw plan levels only if they fall within the visible intraday range.
    # Levels outside the range get an edge annotation instead so they don't squish the chart.
    edge_texts: list[str] = []
    for plan, entry_col, side_lbl in [(long_plan, _LONG_E, "L"), (short_plan, _SHORT_E, "S")]:
        for lkey, style, alpha in [("entry", "-", 0.90), ("stop_loss", "--", 0.55), ("target", "--", 0.55)]:
            lvl = (plan or {}).get(lkey)
            if lvl is None:
                continue
            short_lbl = f"{side_lbl}-{lkey[:1].upper()}: {lvl}"
            x_right = mdates.date2num(pd.to_datetime(d["time"].iloc[-1]).to_pydatetime())
            if y_lo <= lvl <= y_hi:
                ax.axhline(lvl, color=entry_col, linestyle=style, linewidth=1.1, alpha=alpha, zorder=5)
                ax.text(x_right, lvl, f"  {short_lbl}", color=entry_col, fontsize=6.5,
                        va="center", ha="left", clip_on=True)
            else:
                # annotate at top or bottom edge
                arrow = ">>" if lvl > y_hi else "<<"
                edge_texts.append(f"{short_lbl} {arrow}")

    if edge_texts:
        ax.text(0.99, 0.01, "\n".join(edge_texts), transform=ax.transAxes, fontsize=6.5,
                va="bottom", ha="right", color="#334155",
                bbox=dict(facecolor="white", alpha=0.80, edgecolor=_GRID, boxstyle="round,pad=0.3"))

    ax.set_title("Short Term — 15min  (entry / exit timing; plan levels shown as reference)",
                 fontsize=10, color="#1a3a6b", weight="bold", pad=5)
    ax.legend(loc="upper left", fontsize=7, framealpha=0.9)
    ax.set_ylabel("price (INR)", fontsize=8)


def _draw_plan_panel(ax: plt.Axes, daily: pd.DataFrame, zones: dict[str, Any],
                     plan: dict[str, Any], side: str, lookback: int,
                     events: list[dict[str, Any]] | None = None) -> None:
    """Draw a full plan panel (LONG or SHORT) with candlesticks, zones, plan lines, arrow, events."""
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    bg = "#f0fff4" if side == "long" else "#fff5f5"
    ax.set_facecolor(bg)
    ax.grid(True, color=_GRID, linewidth=0.5, zorder=0)
    _candles(ax, d, width_frac=0.65)
    _draw_emas(ax, d, spans=[20, 50, 200])

    # Zone highlighting: active side prominent, other faded
    d_near = 0.50 if side == "long" else 0.06
    d_oth  = 0.28 if side == "long" else 0.03
    s_near = 0.50 if side == "short" else 0.06
    s_oth  = 0.28 if side == "short" else 0.03
    _draw_zones(ax, zones, d_alpha=d_near, s_alpha=s_near, d_other=d_oth, s_other=s_oth)
    nd, ns = zones.get("nearest_demand"), zones.get("nearest_supply")
    x_r = mdates.date2num(pd.to_datetime(d["time"].iloc[-1]).to_pydatetime())
    if nd:
        ax.text(x_r, nd["high"], f"  D{nd['low']}–{nd['high']} s{nd.get('strength','?')}",
                color=_DEMAND, fontsize=7, va="bottom", ha="left", clip_on=True,
                alpha=1.0 if side == "long" else 0.35)
    if ns:
        ax.text(x_r, ns["low"], f"  S{ns['low']}–{ns['high']} s{ns.get('strength','?')}",
                color=_SUPPLY, fontsize=7, va="top", ha="left", clip_on=True,
                alpha=1.0 if side == "short" else 0.35)

    if zones.get("vwap_session") is not None:
        ax.axhline(zones["vwap_session"], color=_VWAP, linestyle=":", linewidth=1.0,
                   label="VWAP", zorder=4)

    # Plan lines + profit fill + directional arrow
    bias = (plan or {}).get("bias")
    if bias in ("long", "short"):
        e_col, s_col, t_col = (_LONG_E, _LONG_S, _LONG_T) if bias == "long" else (_SHORT_E, _SHORT_S, _SHORT_T)
        entry, stop, target = plan.get("entry"), plan.get("stop_loss"), plan.get("target")
        rr, qty = plan.get("risk_reward"), plan.get("quantity")
        fill_col = _DEMAND if bias == "long" else _SUPPLY
        x_l = mdates.date2num(pd.to_datetime(d["time"].iloc[0]).to_pydatetime())
        if entry is not None and target is not None:
            lo, hi = (entry, target) if bias == "long" else (target, entry)
            ax.axhspan(lo, hi, color=fill_col, alpha=0.09, zorder=0)
        for lvl, col, name, lw, ls in [(entry, e_col, "entry", 2.2, "-"),
                                        (stop, s_col, "SL", 1.3, "--"),
                                        (target, t_col, "target", 1.3, "--")]:
            if lvl is not None:
                ax.axhline(lvl, color=col, linestyle=ls, linewidth=lw, zorder=6)
                ax.text(x_l, lvl, f" {name} {lvl} ", color=col, fontsize=9,
                        va="center", ha="left", weight="bold" if name == "entry" else "normal",
                        bbox=dict(facecolor="white", alpha=0.7, pad=1, linewidth=0))
        if entry is not None and target is not None:
            ax.annotate("", xy=(x_r, target), xytext=(x_r, entry),
                        arrowprops=dict(arrowstyle="->", color=e_col, lw=2.2), zorder=7)
        ax.set_xlabel(f"{bias.upper()}  entry {entry}  SL {stop}  target {target}  R:R {rr}  qty {qty}",
                      fontsize=8.5, labelpad=4, color=e_col, weight="bold")
    else:
        ax.text(0.5, 0.5, "No directional setup\n(mid-range or missing zone)",
                transform=ax.transAxes, ha="center", va="center", fontsize=10, color="#aaa",
                bbox=dict(facecolor="white", alpha=0.8, boxstyle="round"))

    _draw_events(ax, daily, lookback, events)
    _mtf_badge(ax, zones)
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.9)
    ax.set_ylabel("price (INR)", fontsize=9)


# ──────────────────────────── public API ────────────────────────────────────

def plot_trade_chart(daily: pd.DataFrame, zones: dict[str, Any], trade_plan: dict[str, Any] | None,
                     path: str | None, title: str | None = None, lookback: int = 160,
                     ax: plt.Axes | None = None, ax_vol: plt.Axes | None = None,
                     events: list[dict[str, Any]] | None = None) -> str | None:
    """Single plan chart (backward-compatible). Standalone = creates figure + volume sub-panel."""
    standalone = ax is None
    if standalone:
        fig = plt.figure(figsize=(14, 8), dpi=110)
        fig.patch.set_facecolor(_BG)
        gs_ = gridspec.GridSpec(2, 1, figure=fig, height_ratios=[4, 1], hspace=0.04)
        ax = fig.add_subplot(gs_[0])
        ax_vol = fig.add_subplot(gs_[1], sharex=ax)
    side = (trade_plan or {}).get("bias", "both")
    if side not in ("long", "short"):
        side = "both"
    _draw_plan_panel(ax, daily, zones, trade_plan or {}, side, lookback, events)
    if title:
        ax.set_title(title, fontsize=11, color=_PRICE, pad=6)
    plt.setp(ax.get_xticklabels(), visible=False)
    if ax_vol is not None:
        _draw_volume(ax_vol, daily, lookback)
    if standalone:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fig.tight_layout()
        fig.savefig(path, facecolor=_BG)
        plt.close(fig)
    return path


def plot_dual_chart(daily: pd.DataFrame, zones: dict[str, Any],
                    long_plan: dict[str, Any], short_plan: dict[str, Any],
                    path: str, symbol: str = "", lookback: int = 160,
                    events: list[dict[str, Any]] | None = None) -> str:
    """Dual plan chart (backward-compatible; same data as quad but no LT/15m rows)."""
    fig = plt.figure(figsize=(24, 10), dpi=110)
    fig.patch.set_facecolor(_BG)
    gs_ = gridspec.GridSpec(2, 2, figure=fig, height_ratios=[4, 1], hspace=0.04, wspace=0.22)
    ax_l, ax_r = fig.add_subplot(gs_[0, 0]), fig.add_subplot(gs_[0, 1])
    axv_l, axv_r = (fig.add_subplot(gs_[1, 0], sharex=ax_l),
                    fig.add_subplot(gs_[1, 1], sharex=ax_r))
    vc = zones.get("volume_context") or {}
    stack = zones.get("ema_stack", "—")
    vwap_s = zones.get("vwap_side") or "—"
    vol_txt = f"  Vol {vc.get('trend','?')} {vc.get('ratio','?')}×" if vc else ""
    for ax, axv, plan, side, accent in [
        (ax_l, axv_l, long_plan,  "long",  _DEMAND),
        (ax_r, axv_r, short_plan, "short", _SUPPLY),
    ]:
        ttxt = f"{'LONG' if side=='long' else 'SHORT'}  PLAN — {symbol}  |  EMA: {stack}  VWAP: {vwap_s}{vol_txt}"
        _draw_plan_panel(ax, daily, zones, plan, side, lookback, events)
        ax.set_title(ttxt, fontsize=11, color=accent, weight="bold", pad=7)
        plt.setp(ax.get_xticklabels(), visible=False)
        _draw_volume(axv, daily, lookback)
    fig.suptitle(f"Trader analysis — {symbol}   LEFT: green=demand(long)   RIGHT: red=supply(short)   "
                 f"volume bars show conviction", fontsize=9.5, color="#334155", y=1.005)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fig.tight_layout(pad=1.5)
    fig.savefig(path, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_quad_chart(daily: pd.DataFrame, intraday_15m: pd.DataFrame | None,
                    zones: dict[str, Any], long_plan: dict[str, Any], short_plan: dict[str, Any],
                    path: str, symbol: str = "", lookback_lt: int = 365, lookback_plan: int = 160,
                    lookback_15m: int = 120, events: list[dict[str, Any]] | None = None) -> str:
    """4-panel quad chart (the default output):

    ┌─────────────────────────┬──────────────────────────┐
    │  Long Term — 1D         │  15-min — entry timing   │
    │  365 bars, EMA200       │  Candlesticks, EMA9/20   │
    │  All zones (faint)      │  Plan levels clamped     │
    ├─────────────────────────┼──────────────────────────┤
    │  LONG plan (green)      │  SHORT plan (red)        │
    │  Candlesticks           │  Candlesticks            │
    │  Entry/SL/Target/arrow  │  Entry/SL/Target/arrow   │
    ├─────────────────────────┼──────────────────────────┤
    │  Volume + MA20 (LONG)   │  Volume + MA20 (SHORT)   │
    └─────────────────────────┴──────────────────────────┘
    """
    fig = plt.figure(figsize=(26, 14), dpi=110)
    fig.patch.set_facecolor(_BG)
    gs_ = gridspec.GridSpec(3, 2, figure=fig, height_ratios=[3.2, 4, 1],
                            hspace=0.22, wspace=0.20)
    ax_lt  = fig.add_subplot(gs_[0, 0])
    ax_15m = fig.add_subplot(gs_[0, 1])
    ax_lp  = fig.add_subplot(gs_[1, 0])
    ax_sp  = fig.add_subplot(gs_[1, 1])
    axv_lp = fig.add_subplot(gs_[2, 0], sharex=ax_lp)
    axv_sp = fig.add_subplot(gs_[2, 1], sharex=ax_sp)

    _draw_longterm(ax_lt, daily, zones, lookback=lookback_lt, events=events)
    _draw_15min(ax_15m, intraday_15m, long_plan, short_plan, lookback=lookback_15m)
    plt.setp(ax_lt.get_xticklabels(), visible=False)
    plt.setp(ax_15m.get_xticklabels(), visible=False)

    vc = zones.get("volume_context") or {}
    stack = zones.get("ema_stack", "—")
    vwap_s = zones.get("vwap_side") or "—"
    vol_txt = f"  Vol {vc.get('trend','?')} {vc.get('ratio','?')}×" if vc else ""
    for ax, axv, plan, side, accent in [
        (ax_lp, axv_lp, long_plan,  "long",  _DEMAND),
        (ax_sp, axv_sp, short_plan, "short", _SUPPLY),
    ]:
        ttxt = f"{'LONG' if side=='long' else 'SHORT'} PLAN — {symbol}  |  EMA: {stack}  VWAP: {vwap_s}{vol_txt}"
        _draw_plan_panel(ax, daily, zones, plan, side, lookback_plan, events)
        ax.set_title(ttxt, fontsize=11, color=accent, weight="bold", pad=6)
        ax.legend(loc="upper left", fontsize=7.5, framealpha=0.9)
        plt.setp(ax.get_xticklabels(), visible=False)
        _draw_volume(axv, daily, lookback_plan)

    fig.suptitle(
        f"Trader analysis — {symbol}   "
        f"Top-left: 1D long-term structure   Top-right: 15min entry timing   "
        f"Bottom: LONG (green) / SHORT (red) plans   volume shows conviction",
        fontsize=9.5, color="#334155", y=1.005)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fig.tight_layout(pad=1.5)
    fig.savefig(path, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    return path
