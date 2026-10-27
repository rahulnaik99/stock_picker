"""Technical chart rendering for the Trader lens.

`plot_trade_chart` / `plot_dual_chart` — original dual plan chart (backward-compat).
`plot_quad_chart` — the full 3-row, 2-column quad chart:
  Row 0 (context):    Long-term daily (1D, 365 bars)  |  Short-term 15-min (entry timing)
  Row 1 (plans):      LONG plan (demand focus)         |  SHORT plan (supply focus)
  Row 2 (volume):     Volume for LONG plan             |  Volume for SHORT plan
+ event markers and MTF badge on every panel.
  LEFT  (LONG)  — demand zones bright green, supply zones faded
  RIGHT (SHORT) — supply zones bright red,  demand zones faded
plus a profit-zone fill and directional arrow so a trader can read it at a glance.
"""
from __future__ import annotations

import warnings
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import matplotlib.gridspec as gridspec   # noqa: E402
import matplotlib.patches as mpatches   # noqa: E402
import pandas as pd                      # noqa: E402

_BG, _GRID, _PRICE = "#ffffff", "#e6e6e6", "#111111"
_EMA = {"ema20": "#e8820c", "ema50": "#1f77b4", "ema200": "#7a3fb0"}
_DEMAND, _SUPPLY, _VWAP = "#1a9850", "#d6301d", "#888888"
_VOL_UP, _VOL_DN = "#1a9850", "#d6301d"
_LONG_E, _LONG_S, _LONG_T = "#1a9850", "#d6301d", "#1f77b4"
_SHORT_E, _SHORT_S, _SHORT_T = "#d6301d", "#1a9850", "#1f77b4"
_EVENT_COLORS = {"results": "#6A0DAD", "dividend": "#1a9850", "board_meeting": "#e8820c",
                 "fundraise": "#1f77b4", "analyst_meet": "#555", "merger_acquisition": "#d6301d"}


# ──────────────────────────────────────────────────── drawing helpers ────

def _draw_events(ax: plt.Axes, daily: pd.DataFrame, lookback: int,
                 events: list[dict[str, Any]] | None) -> None:
    if not events:
        return
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    if d.empty:
        return
    chart_start, chart_end = d["time"].iloc[0], d["time"].iloc[-1]
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
        label = f"{etype.replace('_', ' ').title()} {edate.strftime('%d%b')}"
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


def _draw_base(ax: plt.Axes, daily: pd.DataFrame, zones: dict[str, Any], lookback: int,
               side: str = "both") -> None:
    """Price + EMAs + zone bands + VWAP.
    `side` makes the two dual-chart panels look visually different:
      'long'  → demand zones bright/opaque, supply zones faded
      'short' → supply zones bright/opaque, demand zones faded
      'both'  → equal (single-chart default)"""
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    x = pd.to_datetime(d["time"])
    bg_tint = "#f0fff4" if side == "long" else "#fff5f5" if side == "short" else _BG
    ax.set_facecolor(bg_tint)
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

    # Zone opacity: the "active" side is prominent; the other is nearly invisible
    d_near  = 0.50 if side in ("long", "both") else 0.06
    d_other = 0.28 if side in ("long", "both") else 0.03
    s_near  = 0.50 if side in ("short", "both") else 0.06
    s_other = 0.28 if side in ("short", "both") else 0.03
    d_txt_alpha = 1.0 if side in ("long", "both") else 0.35
    s_txt_alpha = 1.0 if side in ("short", "both") else 0.35

    for z in (zones.get("demand_zones") or []):
        near = nd and abs(z["mid"] - nd["mid"]) < 0.01
        ax.axhspan(z["low"], z["high"], color=_DEMAND, alpha=d_near if near else d_other, zorder=0)
    if nd:
        ax.text(xmax, nd["high"], f"  D {nd['low']}–{nd['high']} s{nd.get('strength','?')}",
                color=_DEMAND, fontsize=7, va="bottom", ha="left", clip_on=True, alpha=d_txt_alpha)
    for z in (zones.get("supply_zones") or []):
        near = ns and abs(z["mid"] - ns["mid"]) < 0.01
        ax.axhspan(z["low"], z["high"], color=_SUPPLY, alpha=s_near if near else s_other, zorder=0)
    if ns:
        ax.text(xmax, ns["low"], f"  S {ns['low']}–{ns['high']} s{ns.get('strength','?')}",
                color=_SUPPLY, fontsize=7, va="top", ha="left", clip_on=True, alpha=s_txt_alpha)
    if zones.get("vwap_session") is not None:
        ax.axhline(zones["vwap_session"], color=_VWAP, linestyle=":", linewidth=1.0, label="VWAP", zorder=2)


def _draw_volume(ax_vol: plt.Axes, daily: pd.DataFrame, lookback: int) -> None:
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
    fill_col = _DEMAND if bias == "long" else _SUPPLY
    d = daily.sort_values("time").tail(lookback)
    xmin = pd.to_datetime(d["time"].iloc[0])
    xmax = pd.to_datetime(d["time"].iloc[-1])
    entry, stop, target = plan.get("entry"), plan.get("stop_loss"), plan.get("target")
    rr, qty = plan.get("risk_reward"), plan.get("quantity")

    # ── profit-zone fill between entry and target (shows P&L corridor visually)
    if entry is not None and target is not None:
        lo, hi = (entry, target) if bias == "long" else (target, entry)
        ax.axhspan(lo, hi, color=fill_col, alpha=0.10, zorder=0)

    # ── plan lines: thick entry, dashed SL/target
    for lvl, col, name, lw, ls in [(entry, e_col, "entry", 2.2, "-"),
                                    (stop, s_col, "SL", 1.3, "--"),
                                    (target, t_col, "target", 1.3, "--")]:
        if lvl is not None:
            ax.axhline(lvl, color=col, linestyle=ls, linewidth=lw, zorder=5)
            ax.text(xmin, lvl, f" {name} {lvl}", color=col, fontsize=9,
                    va="center", ha="left", weight="bold" if name == "entry" else "normal",
                    bbox=dict(facecolor="white", alpha=0.7, pad=1, linewidth=0))

    # ── directional arrow at the right edge
    if entry is not None and target is not None:
        arrow_x = xmax
        ax.annotate("", xy=(arrow_x, target), xytext=(arrow_x, entry),
                    arrowprops=dict(arrowstyle="->", color=e_col, lw=2.0), zorder=6)

    ax.set_xlabel(f"{bias.upper()}  entry {entry}  SL {stop}  target {target}  R:R {rr}  qty {qty}",
                  fontsize=8, labelpad=4, color=e_col, weight="bold")


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


# ──────────────────────────────────────────────────── public API ────

def plot_trade_chart(daily: pd.DataFrame, zones: dict[str, Any], trade_plan: dict[str, Any] | None,
                     path: str | None, title: str | None = None, lookback: int = 160,
                     ax: plt.Axes | None = None, ax_vol: plt.Axes | None = None,
                     events: list[dict[str, Any]] | None = None) -> str | None:
    """Single chart. With no ax/ax_vol, creates its own figure (price + volume sub-panel), saves and returns path.
    When ax is supplied, draws on that axis (used by plot_dual_chart; no save)."""
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
    _draw_base(ax, daily, zones, lookback, side=side)
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
    """Side-by-side dual chart: LEFT = LONG (green-biased), RIGHT = SHORT (red-biased).
    The zone highlighting, background tint, profit fill and directional arrow make the two panels
    immediately distinguishable even at a quick glance."""
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

    for ax, axv, plan, side, title_txt, accent in [
        (ax_l, axv_l, long_plan,  "long",
         f"LONG  —  {symbol}  |  EMA: {stack}  VWAP: {vwap_s}{vol_txt}", _DEMAND),
        (ax_r, axv_r, short_plan, "short",
         f"SHORT —  {symbol}  |  EMA: {stack}  VWAP: {vwap_s}{vol_txt}", _SUPPLY),
    ]:
        _draw_base(ax, daily, zones, lookback, side=side)
        _draw_plan(ax, daily, plan, lookback)
        _draw_events(ax, daily, lookback, events)
        _mtf_badge(ax, zones)
        ax.set_title(title_txt, fontsize=11.5, color=accent, weight="bold", pad=7)
        ax.legend(loc="upper left", fontsize=7.5, framealpha=0.9)
        ax.set_ylabel("price (INR)", fontsize=9)
        plt.setp(ax.get_xticklabels(), visible=False)
        _draw_volume(axv, daily, lookback)

    fig.suptitle(
        f"Trader analysis — {symbol}   "
        f"LEFT: green zones = demand (long setup)   RIGHT: red zones = supply (short setup)   "
        f"volume bars show conviction",
        fontsize=9.5, color="#334155", y=1.005)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fig.tight_layout(pad=1.5)
    fig.savefig(path, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    return path


# ──────────────────────────────────────────────────── quad chart helpers ────

def _draw_longterm(ax: plt.Axes, daily: pd.DataFrame, zones: dict[str, Any],
                   lookback: int = 365) -> None:
    """Long-term daily context panel: more bars, EMA200 prominent, all zones visible, no plan lines."""
    d = daily.sort_values("time").tail(lookback).reset_index(drop=True)
    x = pd.to_datetime(d["time"])
    ax.set_facecolor("#fafffe")  # very faint green tint = "trend / structure"
    ax.grid(True, color=_GRID, linewidth=0.4, zorder=0)
    ax.vlines(x, d["low"], d["high"], color="#c8cfd8", linewidth=0.4, zorder=1)
    ax.plot(x, d["close"], color=_PRICE, linewidth=1.2, label="Close", zorder=3)
    for key, col in _EMA.items():
        span = int(key[3:])
        if len(d) >= span:
            lw = 1.8 if span == 200 else 1.0
            ax.plot(x, d["close"].ewm(span=span, adjust=False).mean(),
                    color=col, linewidth=lw, alpha=0.95, label=key.upper(), zorder=2)
    nd, ns = zones.get("nearest_demand"), zones.get("nearest_supply")
    xmax = x.iloc[-1]
    for z in (zones.get("demand_zones") or []):
        ax.axhspan(z["low"], z["high"], color=_DEMAND, alpha=0.14, zorder=0)
    for z in (zones.get("supply_zones") or []):
        ax.axhspan(z["low"], z["high"], color=_SUPPLY, alpha=0.14, zorder=0)
    if nd:
        ax.text(xmax, nd["high"], f"  D{nd['low']}–{nd['high']}",
                color=_DEMAND, fontsize=6.5, va="bottom", ha="left", clip_on=True)
    if ns:
        ax.text(xmax, ns["low"], f"  S{ns['low']}–{ns['high']}",
                color=_SUPPLY, fontsize=6.5, va="top", ha="left", clip_on=True)
    ax.set_title("Long Term — 1D (big picture, EMA structure, key zones)", fontsize=10,
                 color="#2c5282", weight="bold", pad=5)
    ax.legend(loc="upper left", fontsize=7, framealpha=0.9)
    ax.set_ylabel("price (INR)", fontsize=8)


def _draw_15min(ax: plt.Axes, intraday: pd.DataFrame | None, long_plan: dict[str, Any],
                short_plan: dict[str, Any], lookback: int = 120) -> None:
    """Short-term 15-min panel for entry timing. Shows fast EMAs and plan levels as reference lines."""
    ax.set_facecolor("#f5f7ff")   # faint blue tint = "timing"
    ax.grid(True, color=_GRID, linewidth=0.4, zorder=0)
    if intraday is None or len(intraday) < 10:
        ax.text(0.5, 0.5, "15-min data not available\n(FYERS token required for intraday)",
                transform=ax.transAxes, ha="center", va="center", fontsize=10, color="#888",
                bbox=dict(facecolor="white", alpha=0.8, boxstyle="round"))
        ax.set_title("Short Term — 15min (entry timing)", fontsize=10, color="#1a3a6b", weight="bold", pad=5)
        return
    d = intraday.sort_values("time").tail(lookback).reset_index(drop=True)
    x = pd.to_datetime(d["time"])
    ax.vlines(x, d["low"], d["high"], color="#adb5bd", linewidth=0.5, zorder=1)
    ax.plot(x, d["close"], color=_PRICE, linewidth=1.3, label="Close", zorder=3)
    # Fast EMAs suited to intraday (9 and 20)
    for span, col, lbl in [(9, "#ff6b35", "EMA9"), (20, _EMA["ema20"], "EMA20")]:
        if len(d) >= span:
            ax.plot(x, d["close"].ewm(span=span, adjust=False).mean(),
                    color=col, linewidth=1.0, alpha=0.9, label=lbl, zorder=2)
    # Reference levels from both plans (faint — just for orientation)
    for plan, entry_col, lbl in [(long_plan, _LONG_E, "L"), (short_plan, _SHORT_E, "S")]:
        for lkey, style, alpha in [("entry", "-", 0.85), ("stop_loss", "--", 0.5), ("target", "--", 0.5)]:
            lvl = (plan or {}).get(lkey)
            if lvl is not None:
                ax.axhline(lvl, color=entry_col, linestyle=style, linewidth=1.1, alpha=alpha, zorder=4)
                ax.text(x.iloc[-1], lvl, f"  {lbl}-{lkey[:1].upper()}",
                        color=entry_col, fontsize=6.5, va="center", ha="left", clip_on=True)
    ax.set_title("Short Term — 15min  (entry / exit timing)", fontsize=10,
                 color="#1a3a6b", weight="bold", pad=5)
    ax.legend(loc="upper left", fontsize=7, framealpha=0.9)
    ax.set_ylabel("price (INR)", fontsize=8)


# ──────────────────────────────────────────────────── QUAD CHART (main export) ────

def plot_quad_chart(daily: pd.DataFrame, intraday_15m: pd.DataFrame | None,
                    zones: dict[str, Any], long_plan: dict[str, Any], short_plan: dict[str, Any],
                    path: str, symbol: str = "", lookback_lt: int = 365, lookback_plan: int = 160,
                    lookback_15m: int = 120, events: list[dict[str, Any]] | None = None) -> str:
    """Full 4-panel Trader chart:

    ┌─────────────────────────┬──────────────────────────┐
    │ Long Term — 1D          │ Short Term — 15min       │
    │ Big picture, EMA200     │ Entry timing, fast EMAs  │
    │ All zones visible       │ Plan levels as reference │
    ├─────────────────────────┼──────────────────────────┤
    │ LONG plan               │ SHORT plan               │
    │ GREEN bg, demand focus  │ RED bg, supply focus     │
    │ Entry/SL/Target + arrow │ Entry/SL/Target + arrow  │
    ├─────────────────────────┼──────────────────────────┤
    │ Volume (long plan)      │ Volume (short plan)      │
    └─────────────────────────┴──────────────────────────┘
    """
    fig = plt.figure(figsize=(26, 14), dpi=110)
    fig.patch.set_facecolor(_BG)
    gs_ = gridspec.GridSpec(3, 2, figure=fig, height_ratios=[3.2, 4, 1],
                            hspace=0.22, wspace=0.20)
    # Row 0: context charts (no volume sub-panel to save space)
    ax_lt  = fig.add_subplot(gs_[0, 0])   # long-term daily
    ax_15m = fig.add_subplot(gs_[0, 1])   # 15-min intraday
    # Row 1: plan charts
    ax_lp  = fig.add_subplot(gs_[1, 0])   # LONG plan
    ax_sp  = fig.add_subplot(gs_[1, 1])   # SHORT plan
    # Row 2: volume for plan panels
    axv_lp = fig.add_subplot(gs_[2, 0], sharex=ax_lp)
    axv_sp = fig.add_subplot(gs_[2, 1], sharex=ax_sp)

    # ── Row 0: Long-term context + 15-min timing ──
    _draw_longterm(ax_lt, daily, zones, lookback=lookback_lt)
    _draw_events(ax_lt, daily, lookback_lt, events)
    _mtf_badge(ax_lt, zones)

    _draw_15min(ax_15m, intraday_15m, long_plan, short_plan, lookback=lookback_15m)
    plt.setp(ax_lt.get_xticklabels(), visible=False)
    plt.setp(ax_15m.get_xticklabels(), visible=False)

    # ── Row 1: LONG & SHORT plans ──
    vc = zones.get("volume_context") or {}
    vol_txt = f"  Vol {vc.get('trend','?')} {vc.get('ratio','?')}×" if vc else ""
    stack = zones.get("ema_stack", "—")
    vwap_s = zones.get("vwap_side") or "—"
    for ax, axv, plan, side, ttxt, accent in [
        (ax_lp, axv_lp, long_plan,  "long",
         f"LONG  PLAN — {symbol}  |  EMA: {stack}  VWAP: {vwap_s}{vol_txt}", _DEMAND),
        (ax_sp, axv_sp, short_plan, "short",
         f"SHORT PLAN — {symbol}  |  EMA: {stack}  VWAP: {vwap_s}{vol_txt}", _SUPPLY),
    ]:
        _draw_base(ax, daily, zones, lookback_plan, side=side)
        _draw_plan(ax, daily, plan, lookback_plan)
        _draw_events(ax, daily, lookback_plan, events)
        _mtf_badge(ax, zones)
        ax.set_title(ttxt, fontsize=11, color=accent, weight="bold", pad=6)
        ax.legend(loc="upper left", fontsize=7.5, framealpha=0.9)
        ax.set_ylabel("price (INR)", fontsize=9)
        plt.setp(ax.get_xticklabels(), visible=False)
        _draw_volume(axv, daily, lookback_plan)

    fig.suptitle(
        f"Trader analysis — {symbol}   "
        f"Top: Long-term 1D structure  |  15-min entry timing     "
        f"Bottom: LONG plan (green)  |  SHORT plan (red)   volume shows conviction",
        fontsize=9.5, color="#334155", y=1.01)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fig.tight_layout(pad=1.5)
    fig.savefig(path, facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    return path
