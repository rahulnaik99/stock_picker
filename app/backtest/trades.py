"""Trade-level backtest: turn the Trader's deterministic trade plan into simulated trades and measure them.

For each (symbol, rebalance date) we build the plan point-in-time (entry / stop-loss / target / quantity from the
daily zones up to that date, no look-ahead), then walk the candles FORWARD bar by bar and exit at whichever of the
stop or the target is touched first (if a single bar straddles both, the stop is assumed first - conservative).
We then measure win rate, average R-multiple, expectancy and P&L. This validates the entry/exit/SL/quantity math,
not just a score.
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
    """The trade plan the Trader would have produced on `asof` from daily zones only (no look-ahead). None if the
    history is too short or the zones cannot be computed."""
    d = daily[daily["time"] <= asof]
    if len(d) < min_bars:
        return None
    d = d.tail(window).reset_index(drop=True)
    try:
        z = indicators.demand_supply_zones(d, None)
    except Exception:  # noqa: BLE001
        return None
    return indicators.build_trade_plan(z, capital=capital, risk_pct=risk_pct)


def simulate_trade(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
                   entry_index: int, plan: dict[str, Any], max_bars: int | None = None) -> dict[str, Any] | None:
    """Walk candles forward from `entry_index` and resolve the plan. Entry is assumed filled at `plan['entry']`.
    Long: stop if a bar's low <= stop_loss, target if its high >= target (stop checked first). Short: mirrored.
    Returns outcome ('target'|'stop'|'open'), exit price/index, bars held, per-share and total P&L, and R-multiple.
    None if the plan is not tradable (no side, or zero quantity)."""
    bias, qty = plan.get("bias"), plan.get("quantity", 0)
    if bias not in ("long", "short") or qty <= 0:
        return None
    entry, stop, target, psr = plan["entry"], plan["stop_loss"], plan["target"], plan["per_share_risk"]
    n = len(closes)
    last = n - 1 if max_bars is None else min(n - 1, entry_index + max_bars)
    outcome, exit_price, exit_index = "open", float(closes[last]), last
    for j in range(entry_index + 1, last + 1):
        hi, lo = float(highs[j]), float(lows[j])
        if bias == "long":
            if lo <= stop:
                outcome, exit_price, exit_index = "stop", stop, j; break
            if hi >= target:
                outcome, exit_price, exit_index = "target", target, j; break
        else:
            if hi >= stop:
                outcome, exit_price, exit_index = "stop", stop, j; break
            if lo <= target:
                outcome, exit_price, exit_index = "target", target, j; break
    pnl_ps = (exit_price - entry) if bias == "long" else (entry - exit_price)
    return {"bias": bias, "entry": entry, "stop_loss": stop, "target": target, "quantity": qty,
            "outcome": outcome, "exit_price": round(exit_price, 2), "exit_index": exit_index,
            "bars_held": exit_index - entry_index, "pnl_per_share": round(pnl_ps, 2),
            "pnl": round(pnl_ps * qty, 2), "r_multiple": round(pnl_ps / psr, 3) if psr else None,
            "risk_reward": plan.get("risk_reward")}


def _loc_asof(times: list[pd.Timestamp], asof: pd.Timestamp) -> int | None:
    i = bisect.bisect_right(times, asof) - 1
    return i if i >= 0 else None


def run_trade_backtest(panel: dict[str, pd.DataFrame], rebalance_dates: Sequence[Any], capital: float = 100000.0,
                       risk_pct: float = 1.0, max_bars: int = 63) -> list[dict[str, Any]]:
    """Build + simulate one trade per (symbol, rebalance date) that yields a directional plan. `max_bars` caps how
    long a trade is held before it is marked 'open' and closed at that bar's close."""
    recs: list[dict[str, Any]] = []
    rbs = [pd.Timestamp(d) for d in rebalance_dates]
    for sym, df in panel.items():
        d = df.sort_values("time").reset_index(drop=True)
        times = list(d["time"])
        highs, lows, closes = d["high"].to_numpy(float), d["low"].to_numpy(float), d["close"].to_numpy(float)
        for rb in rbs:
            idx = _loc_asof(times, rb)
            if idx is None:
                continue
            plan = trader_plan_asof(d, times[idx], capital, risk_pct)
            if not plan or plan.get("bias") == "none":
                continue
            sim = simulate_trade(highs, lows, closes, idx, plan, max_bars)
            if sim is None:
                continue
            sim.update({"date": times[idx].date(), "symbol": sym})
            recs.append(sim)
    return recs


def summarize_trades(recs: list[dict[str, Any]]) -> dict[str, Any]:
    """Win rate, expectancy (mean R), average winner/loser R, P&L and holding period over the simulated trades."""
    if not recs:
        return {"n_trades": 0}
    wins = [r for r in recs if r["outcome"] == "target"]
    losses = [r for r in recs if r["outcome"] == "stop"]
    opens = [r for r in recs if r["outcome"] == "open"]
    closed = wins + losses
    rs = [r["r_multiple"] for r in recs if r["r_multiple"] is not None]
    longs = [r for r in recs if r["bias"] == "long"]
    shorts = [r for r in recs if r["bias"] == "short"]
    return {
        "n_trades": len(recs), "wins": len(wins), "losses": len(losses), "open_at_cap": len(opens),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "expectancy_r": round(mean(rs), 3) if rs else None,
        "avg_win_r": round(mean([r["r_multiple"] for r in wins]), 3) if wins else None,
        "avg_loss_r": round(mean([r["r_multiple"] for r in losses]), 3) if losses else None,
        "total_pnl": round(sum(r["pnl"] for r in recs), 2),
        "avg_bars_held": round(mean([r["bars_held"] for r in recs]), 1),
        "avg_risk_reward": round(mean([r["risk_reward"] for r in recs if r.get("risk_reward") is not None]), 2)
        if any(r.get("risk_reward") is not None for r in recs) else None,
        "by_side": {"long": len(longs), "short": len(shorts)},
    }


def trades_to_markdown(summary: dict[str, Any], capital: float, risk_pct: float) -> str:
    if not summary.get("n_trades"):
        return "# Trade backtest\n\nNo directional trade plans were generated over this window.\n"
    s = summary
    pnl_pct = s["total_pnl"] / capital * 100 if capital else 0.0
    L = ["# Trade backtest - Trader plan (entry / stop / target / size), simulated forward", "",
         f"- Capital ₹{capital:,.0f}, risk {risk_pct}%/trade (= ₹{capital * risk_pct / 100:,.0f} risked per trade), hold cap in bars",
         f"- Trades: **{s['n_trades']}** ({s['by_side']['long']} long / {s['by_side']['short']} short)  |  "
         f"target hit: {s['wins']}, stopped: {s['losses']}, open-at-cap: {s['open_at_cap']}",
         f"- **Win rate** (closed): **{_p(s['win_rate'])}**  |  avg planned R:R **{s['avg_risk_reward']}**",
         f"- **Expectancy: {s['expectancy_r']} R per trade**  (avg winner {s['avg_win_r']}R, avg loser {s['avg_loss_r']}R)",
         f"- Total P&L **₹{s['total_pnl']:,.0f}** ({pnl_pct:+.1f}% of capital)  |  avg hold {s['avg_bars_held']} bars", "",
         "_Entry assumed filled at the plan price; a bar touching both stop and target is counted as a stop (conservative). "
         "Daily candles only (no intraday). Not investment advice._"]
    return "\n".join(L)


def _p(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"
