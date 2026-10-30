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
    plan = indicators.build_trade_plan(z, capital=capital, risk_pct=risk_pct)
    if plan:                                  # stash the as-of close + EMA200 for the regime gate (no recompute)
        plan["_close"] = z.get("close")
        plan["_ema200"] = z.get("ema200")
    return plan


# --------------------------------------------------------------------------- pure exit mechanics
def resolve_exit(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], fill_index: int,
                 bias: str, stop: float, target: float | None, max_bars: int | None = None,
                 *, trail_r: float | None = None, psr: float | None = None) -> tuple[str, float, int]:
    """Walk candles forward from `fill_index` and return (outcome, exit_price, exit_index).

    Fixed stop/target (default): stop before target on a straddle bar (conservative). `target=None` means no profit
    cap (used with trailing). If `trail_r` and `psr` are given, a trailing stop rides `trail_r * psr` behind the best
    price seen since entry (never looser than the initial stop); a trailed exit in profit is reported as 'trail'.
    'open' = held to the cap / data end, exit at its close."""
    n = len(closes)
    last = n - 1 if max_bars is None else min(n - 1, fill_index + max_bars)
    trailing = bool(trail_r) and bool(psr)
    best = float(highs[fill_index]) if bias == "long" else float(lows[fill_index])
    dyn = stop
    for j in range(fill_index + 1, last + 1):
        hi, lo = float(highs[j]), float(lows[j])
        # 1) stop/target are evaluated against the level set by PRIOR bars (the stop you carry into bar j) ...
        if bias == "long":
            if lo <= dyn:
                return ("trail" if dyn > stop else "stop"), dyn, j
            if target is not None and hi >= target:
                return "target", target, j
            if trailing:                       # 2) ... then this bar's high ratchets the trail up for the NEXT bar
                best = max(best, hi)
                dyn = max(dyn, best - trail_r * psr)
        else:
            if hi >= dyn:
                return ("trail" if dyn < stop else "stop"), dyn, j
            if target is not None and lo <= target:
                return "target", target, j
            if trailing:
                best = min(best, lo)
                dyn = min(dyn, best + trail_r * psr)
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
                        entry_window: int = 5, allow_short: bool = True, max_positions: int | None = None,
                        min_rr: float = 0.0, target_r: float = 0.0, trail_r: float = 0.0,
                        regime_filter: str = "off", cost_bps: float = 0.0) -> list[dict[str, Any]]:
    """Event-driven, no-leverage, compounding portfolio. Returns executed trades (realised entry/qty/net P&L + dates).

    Risk per trade = risk_pct of CURRENT equity; quantity never exceeds what equity can fund; total open notional
    <= equity; at most one open position per symbol. Trade-quality / exit / realism knobs:
    - `min_rr`        : skip setups whose zone reward-to-risk is below this.
    - `target_r`      : fixed take-profit at N*risk instead of the opposite zone.
    - `trail_r`       : trail the stop N*risk behind the best price ("let winners run").
    - `regime_filter` : 'ema200' = trade only WITH the 200-day trend (longs only when close > EMA200, shorts only
                        when close < EMA200); 'off' = both directions always. Kills counter-trend trades in a bull/bear.
    - `cost_bps`      : per-side cost (brokerage + slippage + taxes) in basis points of notional, charged on entry AND
                        exit and deducted from P&L, so every figure is NET of costs."""
    syms: dict[str, tuple[pd.DataFrame, list, np.ndarray, np.ndarray, np.ndarray]] = {}
    for sym, df in panel.items():
        d = df.sort_values("time").reset_index(drop=True)
        syms[sym] = (d, list(d["time"]), d["high"].to_numpy(float), d["low"].to_numpy(float), d["close"].to_numpy(float))

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
        # Regime gate: only trade with the 200-day trend (drops the counter-trend book).
        if regime_filter == "ema200":
            e200, px0 = plan.get("_ema200"), plan.get("_close")
            if e200 is not None and px0 is not None:
                up = px0 > e200
                if (bias == "long" and not up) or (bias == "short" and up):
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

        stop, zone_target = plan["stop_loss"], plan["target"]
        psr = (fill_price - stop) if bias == "long" else (stop - fill_price)
        zone_reward = (zone_target - fill_price) if bias == "long" else (fill_price - zone_target)
        if psr <= 0 or zone_reward <= 0:       # market fill broke the plan geometry -> no sensible trade
            continue
        if (zone_reward / psr) < min_rr:        # quality filter on the setup's reward-to-risk
            continue

        # Exit target: fixed R multiple, or the opposite zone, or None (pure trailing lets the winner run).
        if target_r > 0:
            target = fill_price + target_r * psr if bias == "long" else fill_price - target_r * psr
        elif trail_r > 0:
            target = None
        else:
            target = zone_target
        reward = None if target is None else (target - fill_price if bias == "long" else fill_price - target)

        risk_budget = equity * risk_pct / 100.0
        used = sum(p["notional"] for p in open_pos.values())
        avail = max(0.0, equity - used)
        qty = min(int(risk_budget // psr), int(avail // fill_price))
        if qty <= 0:
            continue

        outcome, exit_price, exit_index = resolve_exit(H, L, C, fill_index, bias, stop, target, max_bars,
                                                       trail_r=(trail_r or None), psr=psr)
        gross_ps = (exit_price - fill_price) if bias == "long" else (fill_price - exit_price)
        cost = (fill_price + exit_price) * qty * cost_bps / 10000.0   # per-side bps on entry + exit notional
        pnl = round(gross_ps * qty - cost, 2)
        open_pos[sym] = {"exit_date": times[exit_index], "pnl": pnl, "notional": round(qty * fill_price, 2)}
        trades.append({
            "entry_date": times[fill_index], "exit_date": times[exit_index], "symbol": sym, "bias": bias,
            "entry": round(fill_price, 2), "stop_loss": round(stop, 2),
            "target": None if target is None else round(target, 2),
            "risk_reward": round((reward / psr) if reward else (zone_reward / psr), 2), "quantity": qty,
            "notional": round(qty * fill_price, 2), "outcome": outcome, "exit_price": round(exit_price, 2),
            "bars_held": exit_index - fill_index, "cost": round(cost, 2),
            "pnl_per_share": round(gross_ps, 2), "pnl": pnl, "r_multiple": round((pnl / qty) / psr, 3)})
    for pos in open_pos.values():             # close any still-open at the loop end
        equity += pos["pnl"]
    return trades


# --------------------------------------------------------------------------- metrics + report
def _perf(equities: list[float], start: Any, end: Any, capital: float) -> dict[str, Any]:
    """Final / total-return / CAGR / max-drawdown from an equity series that starts at `capital`."""
    final = equities[-1]
    peak, maxdd = capital, 0.0
    for e in equities:
        peak = max(peak, e)
        maxdd = min(maxdd, e / peak - 1.0)
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1e-9)
    cagr = (final / capital) ** (1 / years) - 1 if final > 0 else -1.0
    return {"final_equity": round(final, 2), "total_return_pct": round((final / capital - 1) * 100, 2),
            "cagr_pct": round(cagr * 100, 2), "max_drawdown_pct": round(maxdd * 100, 2)}


def equity_curve_rows(trades: list[dict[str, Any]], capital: float) -> list[tuple[Any, float]]:
    """(exit_date, equity) timeline from realising each trade's P&L at its exit date. Starts at (None, capital)."""
    return _equity_curve(trades, capital)


def _equity_curve(trades: list[dict[str, Any]], capital: float) -> list[tuple[Any, float]]:
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
    start = min(t["entry_date"] for t in trades)
    end = max(t["exit_date"] for t in trades)
    perf = _perf([e for _, e in curve], start, end, capital)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    closed = [t for t in trades if t["outcome"] != "open"]        # target / stop / trail
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    return {
        "n_trades": len(trades), "capital": capital, **perf,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "win_rate": round(sum(1 for t in closed if t["pnl"] > 0) / len(closed), 3) if closed else None,
        "expectancy_r": round(mean(rs), 3) if rs else None,
        "avg_win_r": round(mean([t["r_multiple"] for t in wins]), 3) if wins else None,
        "avg_loss_r": round(mean([t["r_multiple"] for t in losses]), 3) if losses else None,
        "avg_risk_reward": round(mean([t["risk_reward"] for t in trades]), 2),
        "avg_bars_held": round(mean([t["bars_held"] for t in trades]), 1),
        "total_cost": round(sum(t.get("cost", 0.0) for t in trades), 2),
        "open_at_cap": sum(1 for t in trades if t["outcome"] == "open"),
        "outcomes": {k: sum(1 for t in trades if t["outcome"] == k) for k in ("target", "trail", "stop", "open")},
        "by_side": {"long": sum(1 for t in trades if t["bias"] == "long"),
                    "short": sum(1 for t in trades if t["bias"] == "short")},
        "period": [str(start.date()) if hasattr(start, "date") else str(start),
                   str(end.date()) if hasattr(end, "date") else str(end)],
    }


def benchmark_buy_and_hold(panel: dict[str, pd.DataFrame], capital: float,
                           symbol: str | None = None) -> dict[str, Any] | None:
    """Buy-and-hold benchmark over the SAME period: a single `symbol` if given and present, else an equal-weight
    basket of the whole panel (each name normalised to its first close). Returns perf + the equity curve, or None."""
    if not panel:
        return None
    if symbol and symbol in panel:
        s = panel[symbol].set_index("time")["close"].sort_index()
        basket = (s / s.iloc[0]).dropna()
        label = symbol
    else:
        norm = []
        for sym, df in panel.items():
            s = df.set_index("time")["close"].sort_index()
            if len(s):
                norm.append((s / s.iloc[0]).rename(sym))
        if not norm:
            return None
        mat = pd.concat(norm, axis=1).sort_index().dropna()
        if mat.empty:
            return None
        basket = mat.mean(axis=1)
        basket = basket / basket.iloc[0]
        label = f"equal-weight basket ({mat.shape[1]} names)"
    eq = (capital * basket).tolist()
    perf = _perf(eq, basket.index[0], basket.index[-1], capital)
    perf["label"] = label
    perf["curve"] = [(str(ts.date()), round(v, 2)) for ts, v in zip(basket.index, eq)]
    return perf


def portfolio_to_markdown(summary: dict[str, Any], risk_pct: float, entry_mode: str,
                          knobs: dict[str, Any] | None = None, benchmark: dict[str, Any] | None = None) -> str:
    if not summary.get("n_trades"):
        return "# Trade backtest\n\nNo tradable plans were generated / filled over this window.\n"
    s = summary
    knob_txt = ""
    if knobs:
        parts = [f"min_rr={knobs.get('min_rr')}" if knobs.get("min_rr") else "",
                 f"target={knobs.get('target_r')}R" if knobs.get("target_r") else "",
                 f"trail={knobs.get('trail_r')}R" if knobs.get("trail_r") else "",
                 f"regime={knobs.get('regime')}" if knobs.get("regime") and knobs.get("regime") != "off" else "",
                 f"cost={knobs.get('cost_bps')}bps/side" if knobs.get("cost_bps") else ""]
        knob_txt = "  |  " + ", ".join(p for p in parts if p) if any(parts) else ""
    L = ["# Trade backtest - Trader plan, realistic portfolio simulation", "",
         f"- Capital ₹{s['capital']:,.0f}, risk {risk_pct}% of **current equity**/trade, **{entry_mode}** entry, "
         f"no leverage, 1 position per symbol{knob_txt}", f"- Period: {s['period'][0]} -> {s['period'][1]}", "",
         f"- Trades: **{s['n_trades']}** ({s['by_side']['long']} long / {s['by_side']['short']} short); "
         f"outcomes {s['outcomes']}  |  avg planned R:R {s['avg_risk_reward']}, avg hold {s['avg_bars_held']} bars"
         f"  |  costs ₹{s.get('total_cost', 0):,.0f}",
         f"- **Final equity ₹{s['final_equity']:,.0f}**  (total **{s['total_return_pct']:+.1f}%**, **CAGR {s['cagr_pct']:+.1f}%**, net of costs)",
         f"- **Max drawdown {s['max_drawdown_pct']:.1f}%**  |  **profit factor {s['profit_factor']}**",
         f"- Win rate {_p(s['win_rate'])}  |  expectancy **{s['expectancy_r']} R**  (avg win {s['avg_win_r']}R, avg loss {s['avg_loss_r']}R)"]
    if benchmark:
        edge = s["cagr_pct"] - benchmark["cagr_pct"]
        L += ["", f"- **Benchmark (buy & hold, {benchmark['label']})**: total {benchmark['total_return_pct']:+.1f}%, "
              f"CAGR {benchmark['cagr_pct']:+.1f}%, max drawdown {benchmark['max_drawdown_pct']:.1f}%",
              f"- **Strategy vs benchmark: {edge:+.1f}% CAGR** ({'OUTPERFORMS' if edge > 0 else 'UNDERPERFORMS'})"]
    L += ["", "_Entry at the signal bar's close (market) or a touched limit; stop before target on a straddle bar; "
          "risk compounds on realised equity; daily candles only. Not investment advice._"]
    return "\n".join(L)


def _p(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"
