"""Trader Agent: price-action + demand/supply zones + upcoming events + institutional flow.

Distinct from the Indicator agent (EMA/RSI/MACD scoring): this one reasons in zones, EMA, VWAP, volume,
upcoming catalysts (results / dividends / board meetings), block / bulk deals, and FII/DII flows —
both the technical setup AND the near-term event calendar that can amplify or invalidate a zone reaction.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.Agents.base import analyze, load_prompt, unavailable_result
from app.mcp_server.client import AgentTools
from app.mcp_server.tools.common import parse_as_of
from app.schemas import AgentResult
from app.scoring import baselines

NAME = "trader"
TOOLS = ["get_demand_supply_zones", "nse_company_events", "nse_deals", "nse_fii_dii"]

_EVENT_TYPES_UPCOMING = ("results", "dividend", "board_meeting", "fundraise", "merger_acquisition", "analyst_meet")

def _build_event_context(events_data: dict[str, Any], deals_data: dict[str, Any],
                         fii_dii: dict[str, Any], as_of_dt) -> dict[str, Any]:
    """Condense the three event sources into a small, prompt-friendly context dict."""
    today = as_of_dt.date() if hasattr(as_of_dt, "date") else date.today()
    horizon = (today + timedelta(days=45)).isoformat()

    # Upcoming catalysts within 45 days
    upcoming: list[dict[str, Any]] = []
    past_events: list[dict[str, Any]] = []
    if events_data.get("status") == "ok":
        for e in (events_data.get("data") or []):
            edate = str(e.get("event_date") or "")
            if not edate:
                continue
            etype = e.get("event_type", "other")
            summary = (e.get("event_summary") or "")[:90]
            if edate >= today.isoformat() and edate <= horizon and etype in _EVENT_TYPES_UPCOMING:
                upcoming.append({"type": etype, "date": edate, "summary": summary})
            elif edate < today.isoformat() and etype in ("results", "dividend", "board_meeting"):
                past_events.append({"type": etype, "date": edate, "summary": summary})
    upcoming = sorted(upcoming, key=lambda x: x["date"])[:6]
    past_events = sorted(past_events, key=lambda x: x["date"], reverse=True)[:4]

    # Block / bulk deals
    recent_deals: list[dict[str, Any]] = []
    if deals_data.get("status") == "ok":
        for d in (deals_data.get("data") or [])[:6]:
            recent_deals.append({"type": d.get("type"), "date": d.get("date"),
                                  "client": d.get("client"), "side": d.get("side"),
                                  "quantity": d.get("quantity"), "avg_price": d.get("avg_price")})
    deal_buys = sum(1 for d in recent_deals if str(d.get("side", "")).upper() in ("B", "BUY", "BT", "BUY/"))
    deal_sells = sum(1 for d in recent_deals if str(d.get("side", "")).upper() in ("S", "SELL", "ST", "SELL/"))
    deal_lean = "net_buy" if deal_buys > deal_sells else "net_sell" if deal_sells > deal_buys else "neutral"

    # FII / DII institutional flow (market-wide)
    fii_ctx: dict[str, Any] = {}
    if fii_dii.get("status") == "ok":
        analytics = (fii_dii.get("data") or {}).get("analytics", {})
        fii_a = analytics.get("FII", {})
        dii_a = analytics.get("DII", {})
        fii_net = fii_a.get("net_1d") if isinstance(fii_a.get("net_1d"), (int, float)) else None
        dii_net = dii_a.get("net_1d") if isinstance(dii_a.get("net_1d"), (int, float)) else None
        if fii_net is not None or dii_net is not None:
            combined = (fii_net or 0) + (dii_net or 0)
            fii_ctx = {"fii_net_1d_cr": fii_net, "dii_net_1d_cr": dii_net, "combined_net_cr": round(combined, 2),
                       "lean": "net_buying" if combined > 500 else "net_selling" if combined < -500 else "neutral",
                       "scope": "market_wide_not_stock_specific"}

    return {
        "upcoming_catalysts": upcoming,
        "recent_past_events": past_events,
        "block_bulk_deals": recent_deals,
        "deal_lean": deal_lean,
        "fii_dii": fii_ctx,
        "note": ("Upcoming catalysts within 45 days. Block/bulk deals are stock-specific; FII/DII is market-wide only "
                 "— do NOT attribute FII flow directly to this stock. Dividend near demand = potential support buy; "
                 "results near a key zone = volatility catalyst; large block sell near supply = bearish confluence."),
    }


async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    z = await tools.call("get_demand_supply_zones", symbol=symbol, as_of=as_of)
    events_data = await tools.call("nse_company_events", symbol=symbol, days=90, as_of=as_of)
    deals_data = await tools.call("nse_deals", symbol=symbol, days=30, as_of=as_of)
    fii_dii = await tools.call("nse_fii_dii", as_of=as_of)
    results = {"get_demand_supply_zones": z, "nse_company_events": events_data,
               "nse_deals": deals_data, "nse_fii_dii": fii_dii}

    if z["status"] != "ok":
        return unavailable_result(NAME, symbol, results, z.get("message") or "demand/supply zones unavailable")
    data = z["data"]
    event_ctx = _build_event_context(events_data, deals_data, fii_dii, parse_as_of(as_of))
    base, parts = baselines.trader_baseline(data, event_ctx=event_ctx)

    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__),
                        evidence={"zones": data, "event_context": event_ctx},
                        tool_results=results, context={**meta, "as_of": as_of}, baseline=base)
    if res.status == "ok":
        res.extra.update({
            "bull_score": parts.get("bull_score"), "bear_score": parts.get("bear_score"),
            "ema_stack": data.get("ema_stack"), "vwap_side": data.get("vwap_side"),
            "position": (data.get("price_action") or {}).get("position"),
            "nearest_demand": data.get("nearest_demand"), "nearest_supply": data.get("nearest_supply"),
            "volume_context": data.get("volume_context"),
            "mtf": data.get("mtf"), "trade_plan": data.get("trade_plan"),
            "long_plan": data.get("long_plan"), "short_plan": data.get("short_plan"),
            "event_context": event_ctx, "candle_provider": data.get("candle_provider"),
        })
        if data.get("vwap_session") is None:
            res.missing_data.append("session VWAP (no intraday volume)")
        if events_data.get("status") != "ok":
            res.missing_data.append("upcoming catalyst calendar (NSE events unavailable)")
    return res
