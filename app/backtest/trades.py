"""Trade-level backtest for the Trader's plan - with realistic fills, position sizing and a compounding equity curve.

Why this exists: an "independent trades, optimistic fill" simulation massively overstates results (it can assume a
limit entry below the market fills without price ever trading there, size unfunded quantity off a tiny stop, and sum
P&L across overlapping trades as if each were separately capitalised). This module avoids all three:

- ENTRY REALISM: default `entry_mode="market"` fills at the signal bar's CLOSE (no fictitious better price). The
  optional `entry_mode="limit"` only fills if price actually trades to the plan's entry within `entry_window` bars,
  else the trade is a `no_fill` and is not counted.
- NO UNFUNDED LEVERAGE: quantity = min(risk_budget / per_share_risk, affordable_shares), and total open notional is
  capped at current equity.
- SEQUENTIAL PORTFOLIO: at most one open position per symbol; risk per trade = risk_pct of CURRENT (realised) equity,
  so returns compound. The equity curve gives CAGR, max drawdown and profit factor - not a meaningless "% of capital".

`resolve_exit` / `simulate_trade` are the pure exit mechanics (unit-tested); `run_trade_portfolio` is the realistic
event loop.
"""
from __future__ import annotations

import bisect
from datetime import date
from statistics import mean
from typing import Any, Sequence

import numpy as np
import pandas as pd

from app.mcp_server.tools import indicators


def trader_plan_asof(daily: pd.DataFrame, asof: pd.Timestamp, capital: float = 100000.0, risk_pct: float = 1.0,
                     min_bars: int = 60, window: int = 420) -> dict[str, Any] | None:
    """The trade plan the Trader would have produced on `asof` from daily zones only (no look-ahead)."""
    d = daily[daily["time"] <= asof]
    if len(d) < min_bars:
        return None
    d = d.tail(window).reset_index(drop=True)
    try:
        z = indicators.demand_supply_zones(d, None)
    except Exception:  # noqa: BLE001
        return None
    return indicators.build_trade_plan(z, capital=capital, risk_pct=risk_pct)


# --------------------------------------------------------------------------- pure exit mechanics
def resolve_exit(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], fill_index: int,
                 bias: str, stop: float, target: float, max_bars: int | None = None) -> tuple[str, float, int]:
    """Walk candles forward from `fill_index` and return (outcome, exit_price, exit_index). A bar touching both the
    stop and the target is resolved as a STOP (conservative). 'open' = held to the cap / data end, exit at its close."""
    n = len(closes)
    last = n - 1 if max_bars is None else min(n - 1, fill_index + max_bars)
    for j in range(fill_index + 1, last + 1):
        hi, lo = float(highs[j]), float(lows[j])
        if bias == "long":
            if lo <= stop:
                return "stop", stop, j
            if hi >= target:
                return "target", target, j
        else:
            if hi >= stop:
                return "stop", stop, j
            if lo <= target:
                return "target", target, j
    return "open", float(closes[last]), last


def simulate_trade(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
                   entry_index: int, plan: dict[str, Any], max_bars: int | None = None) -> dict[str, Any] | None:
    """Single-trade mechanics for a FIXED plan (entry at plan['entry'], plan['quantity'] shares). Used by the unit
    tests and simple checks. Returns outcome + per-share and total P&L + R-multiple. None if not tradable."""
    bias, qty = plan.get("bias"), plan.get("quantity", 0)
    if bias not in ("long", "short") or qty <= 0:
        return None
    entry, stop, target, psr = plan["entry"], plan["stop_loss"], plan["target"], plan["per_share_risk"]
    outcome, exit_price, exit_index = resolve_exit(highs, lows, closes, entry_index, bias, stop, target, max_bars)
    pnl_ps = (exit_price - entry) if bias == "long" else (entry - exit_price)
    return {"bias": bias, "entry": entry, "stop_loss": stop, "target": target, "quantity": qty,
            "outcome": outcome, "exit_price": round(exit_price, 2), "exit_index": exit_index,
            "bars_held": exit_index - entry_index, "pnl_per_share": round(pnl_ps, 2),
            "pnl": round(pnl_ps * qty, 2), "r_multiple": round(pnl_ps / psr, 3) if psr else None,
            "risk_reward": plan.get("risk_reward")}


def _loc_asof(times: list[pd.Timestamp], asof: pd.Timestamp) -> int | None:
    i = bisect.bisect_right(times, asof) - 1
    return i if i >= 0 else None


def _limit_fill(highs: Sequence[float], lows: Sequence[float], start: int, bias: str, entry: float,
                window: int, max_index: int) -> int | None:
    """First bar after `start` (within `window`) where price trades to the limit `entry`. None if never touched."""
    for j in range(start + 1, min(max_index, start + window) + 1):
        if bias == "long" and float(lows[j]) <= entry:
            return j
        if bias == "short" and float(highs[j]) >= entry:
            return j
    return None


# --------------------------------------------------------------------------- realistic portfolio event loop
def run_trade_portfolio(panel: dict[str, pd.DataFrame], rebalance_dates: Sequence[Any], capital: float = 100000.0,
                        risk_pct: float = 1.0, max_bars: int = 63, entry_mode: str = "market",
                        entry_window: int = 5, allow_short: bool = True,
                        max_positions: int | None = None) -> list[dict[str, Any]]:
    """Event-driven, no-leverage, compounding portfolio. Returns the list of executed trades (each with the realised
    entry/qty/P&L and its entry/exit dates). Risk per trade = risk_pct of CURRENT equity; quantity never exceeds what
    equity can fund; total open notional <= equity; at most one open position per symbol."""
    syms: dict[str, tuple[pd.DataFrame, list, np.ndarray, np.ndarray, np.ndarray]] = {}
    for sym, df in panel.items():
        d = df.sort_values("time").reset_index(drop=True)
        syms[sym] = (d, list(d["time"]), d["high"].to_numpy(float), d["low"].to_numpy(float), d["close"].to_numpy(float))

    # Build directional signals, then process strictly in date order.
    signals: list[tuple[pd.Timestamp, int, str, dict]] = []
    for sym, (d, times, H, L, C) in syms.items():
        for rb in [pd.Timestamp(x) for x in rebalance_dates]:
            idx = _loc_asof(times, rb)
            if idx is None:
                continue
            plan = trader_plan_asof(d, times[idx], capital, risk_pct)
            if plan and plan.get("bias") in ("long", "short"):
                signals.append((times[idx], idx, sym, plan))
    signals.sort(key=lambda s: (s[0], s[2]))

    equity = capital
    open_pos: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []

    def _close_due(upto: pd.Timestamp) -> None:
        nonlocal equity
        for s, pos in list(open_pos.items()):
            if pos["exit_date"] <= upto:
                equity += pos["pnl"]
                del open_pos[s]

    for sig_date, idx, sym, plan in signals:
        _close_due(sig_date)
        if sym in open_pos:
            continue
        bias = plan["bias"]
        if bias == "short" and not allow_short:
            continue
        if max_positions is not None and len(open_pos) >= max_positions:
            continue
        d, times, H, L, C = syms[sym]
        last_index = len(C) - 1

        if entry_mode == "market":
            fill_index, fill_price = idx, float(C[idx])
        else:  # limit: require the plan entry to be touched
            fj = _limit_fill(H, L, idx, bias, plan["entry"], entry_window, last_index)
            if fj is None:
                continue
            fill_index, fill_price = fj, plan["entry"]

        stop, target = plan["stop_loss"], plan["target"]
        psr = (fill_price - stop) if bias == "long" else (stop - fill_price)
        reward = (target - fill_price) if bias == "long" else (fill_price - target)
        if psr <= 0 or reward <= 0:            # market fill broke the plan geometry -> no sensible trade
            continue
        risk_budget = equity * risk_pct / 100.0
        used = sum(p["notional"] for p in open_pos.values())
        avail = max(0.0, equity - used)
        qty = min(int(risk_budget // psr), int(avail // fill_price))
        if qty <= 0:
            continue

        outcome, exit_price, exit_index = resolve_exit(H, L, C, fill_index, bias, stop, target, max_bars)
        pnl_ps = (exit_price - fill_price) if bias == "long" else (fill_price - exit_price)
        pnl = round(pnl_ps * qty, 2)
        open_pos[sym] = {"exit_date": times[exit_index], "pnl": pnl, "notional": round(qty * fill_price, 2)}
        trades.append({
            "entry_date": times[fill_index], "exit_date": times[exit_index], "symbol": sym, "bias": bias,
            "entry": round(fill_price, 2), "stop_loss": round(stop, 2), "target": round(target, 2),
            "risk_reward": round(reward / psr, 2), "quantity": qty, "notional": round(qty * fill_price, 2),
            "outcome": outcome, "exit_price": round(exit_price, 2), "bars_held": exit_index - fill_index,
            "pnl_per_share": round(pnl_ps, 2), "pnl": pnl, "r_multiple": round(pnl_ps / psr, 3)})
    for pos in open_pos.values():             # close any still-open at the loop end
        equity += pos["pnl"]
    return trades


# --------------------------------------------------------------------------- metrics + report
def _equity_curve(trades: list[dict[str, Any]], capital: float) -> list[tuple[Any, float]]:
    """Realise each trade's P&L at its exit date; return the running-equity timeline (sorted by exit date)."""
    eq = capital
    curve = [(None, capital)]
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        eq = round(eq + t["pnl"], 2)
        curve.append((t["exit_date"], eq))
    return curve


def summarize_portfolio(trades: list[dict[str, Any]], capital: float) -> dict[str, Any]:
    """Realistic portfolio metrics: final equity, total return, CAGR, max drawdown, profit factor, win rate,
    expectancy (R), average winner/loser R, holding period and long/short split."""
    if not trades:
        return {"n_trades": 0, "capital": capital}
    curve = _equity_curve(trades, capital)
    equities = [e for _, e in curve]
    final = equities[-1]
    peak, maxdd = capital, 0.0
    for e in equities:
        peak = max(peak, e)
        maxdd = min(maxdd, e / peak - 1.0)
    start = min(t["entry_date"] for t in trades)
    end = max(t["exit_date"] for t in trades)
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1e-9)
    cagr = (final / capital) ** (1 / years) - 1 if final > 0 else -1.0

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    closed = [t for t in trades if t["outcome"] in ("target", "stop")]
    tgt = [t for t in closed if t["outcome"] == "target"]
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    return {
        "n_trades": len(trades), "capital": capital, "final_equity": round(final, 2),
        "total_return_pct": round((final / capital - 1) * 100, 2), "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(maxdd * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "win_rate": round(len(tgt) / len(closed), 3) if closed else None,
        "expectancy_r": round(mean(rs), 3) if rs else None,
        "avg_win_r": round(mean([t["r_multiple"] for t in wins]), 3) if wins else None,
        "avg_loss_r": round(mean([t["r_multiple"] for t in losses]), 3) if losses else None,
        "avg_risk_reward": round(mean([t["risk_reward"] for t in trades]), 2),
        "avg_bars_held": round(mean([t["bars_held"] for t in trades]), 1),
        "open_at_cap": sum(1 for t in trades if t["outcome"] == "open"),
        "by_side": {"long": sum(1 for t in trades if t["bias"] == "long"),
                    "short": sum(1 for t in trades if t["bias"] == "short")},
        "period": [str(start.date()) if hasattr(start, "date") else str(start),
                   str(end.date()) if hasattr(end, "date") else str(end)],
    }


def portfolio_to_markdown(summary: dict[str, Any], risk_pct: float, entry_mode: str) -> str:
    if not summary.get("n_trades"):
        return "# Trade backtest\n\nNo tradable plans were generated / filled over this window.\n"
    s = summary
    L = ["# Trade backtest - Trader plan, realistic portfolio simulation", "",
         f"- Capital ₹{s['capital']:,.0f}, risk {risk_pct}% of **current equity** per trade, **{entry_mode}** entry, "
         f"no leverage (open notional <= equity), 1 position per symbol", f"- Period: {s['period'][0]} -> {s['period'][1]}", "",
         f"- Trades: **{s['n_trades']}** ({s['by_side']['long']} long / {s['by_side']['short']} short), "
         f"open-at-cap {s['open_at_cap']}  |  avg planned R:R {s['avg_risk_reward']}, avg hold {s['avg_bars_held']} bars",
         f"- **Final equity ₹{s['final_equity']:,.0f}**  (total **{s['total_return_pct']:+.1f}%**, **CAGR {s['cagr_pct']:+.1f}%**)",
         f"- **Max drawdown {s['max_drawdown_pct']:.1f}%**  |  **profit factor {s['profit_factor']}**",
         f"- Win rate {_p(s['win_rate'])}  |  expectancy **{s['expectancy_r']} R**  (avg win {s['avg_win_r']}R, avg loss {s['avg_loss_r']}R)",
         "", "_Entry at the signal bar's close (market) or a touched limit; stop before target on a straddle bar "
         "(conservative); risk compounds on realised equity; daily candles only. Not investment advice._"]
    return "\n".join(L)


def _p(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"
