"""Trader Agent: zones + MTF + events + option chain + fundamental snapshot.

All five evidence streams are synthesised before the LLM receives a single prompt:
  1. Demand / supply zones  (get_demand_supply_zones) — WHERE price reacts
  2. Upcoming events + FII/deals (nse_company_events, nse_deals, nse_fii_dii) — WHEN and by WHOM
  3. Option chain snapshot (get_option_chain) — what DERIVATIVES positioning says about demand/supply
  4. Fundamental snapshot (quarterly_fundamentals) — is the company worth trading LONG at all?
Code digests each into a compact block; the LLM calibrates the zone read against all four.
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
TOOLS = [
    "get_demand_supply_zones",
    "nse_company_events",
    "nse_deals",
    "nse_fii_dii",
    "get_option_chain",
    "quarterly_fundamentals",
]

_EVENT_TYPES_UPCOMING = ("results", "dividend", "board_meeting", "fundraise",
                         "merger_acquisition", "analyst_meet")
_LENDER_PREFIXES = ("bank_", "nbfc", "housing_finance", "govt_financier")


# ──────────────────────── digest builders (code, not LLM) ────────────────────

def _build_event_context(events_data: dict, deals_data: dict,
                         fii_dii: dict, as_of_dt) -> dict[str, Any]:
    today = as_of_dt.date() if hasattr(as_of_dt, "date") else date.today()
    horizon = (today + timedelta(days=45)).isoformat()
    upcoming, past_events = [], []
    if events_data.get("status") == "ok":
        for e in (events_data.get("data") or []):
            edate = str(e.get("event_date") or "")
            etype = e.get("event_type", "other")
            summary = (e.get("event_summary") or "")[:90]
            if edate >= today.isoformat() and edate <= horizon and etype in _EVENT_TYPES_UPCOMING:
                upcoming.append({"type": etype, "date": edate, "summary": summary})
            elif edate < today.isoformat() and etype in ("results", "dividend", "board_meeting"):
                past_events.append({"type": etype, "date": edate, "summary": summary})
    upcoming = sorted(upcoming, key=lambda x: x["date"])[:6]
    past_events = sorted(past_events, key=lambda x: x["date"], reverse=True)[:4]
    recent_deals = []
    if deals_data.get("status") == "ok":
        for d in (deals_data.get("data") or [])[:6]:
            recent_deals.append({"type": d.get("type"), "date": d.get("date"),
                                  "client": d.get("client"), "side": d.get("side"),
                                  "quantity": d.get("quantity"), "avg_price": d.get("avg_price")})
    deal_buys  = sum(1 for d in recent_deals if str(d.get("side","")).upper() in ("B","BUY","BT","BUY/"))
    deal_sells = sum(1 for d in recent_deals if str(d.get("side","")).upper() in ("S","SELL","ST","SELL/"))
    deal_lean = "net_buy" if deal_buys > deal_sells else "net_sell" if deal_sells > deal_buys else "neutral"
    fii_ctx: dict[str, Any] = {}
    if fii_dii.get("status") == "ok":
        analytics = (fii_dii.get("data") or {}).get("analytics", {})
        fii_n = analytics.get("FII", {}).get("net_1d")
        dii_n = analytics.get("DII", {}).get("net_1d")
        if isinstance(fii_n, (int, float)) or isinstance(dii_n, (int, float)):
            combined = (fii_n or 0) + (dii_n or 0)
            fii_ctx = {"fii_net_1d_cr": fii_n, "dii_net_1d_cr": dii_n, "combined_net_cr": round(combined, 2),
                       "lean": "net_buying" if combined > 500 else "net_selling" if combined < -500 else "neutral",
                       "scope": "market_wide_not_stock_specific"}
    return {"upcoming_catalysts": upcoming, "recent_past_events": past_events,
            "block_bulk_deals": recent_deals, "deal_lean": deal_lean, "fii_dii": fii_ctx,
            "note": "block/bulk deals = stock-specific; FII/DII = market-wide only."}


def _build_option_ctx(chain_data: dict, zones: dict) -> dict[str, Any]:
    """Compact option-chain digest: PCR, max pain, call wall, put floor, OI quadrant.
    Flags when option levels coincide with demand/supply zones (strong confluence)."""
    if chain_data.get("status") != "ok":
        return {"status": chain_data.get("status", "error"), "note": "option data unavailable"}
    d = chain_data.get("data") or {}
    px = d.get("underlying_ltp")
    pcr = d.get("pcr_oi")
    max_pain = (d.get("levels") or {}).get("max_pain_approx")
    call_wall = next((z["strike"] for z in (d.get("levels") or {}).get("resistance") or []), None)
    put_floor  = next((z["strike"] for z in (d.get("levels") or {}).get("support") or []), None)
    dp = d.get("underlying_change_pct", 0)
    doi = (d.get("day_call_oi_change") or 0) + (d.get("day_put_oi_change") or 0)
    quadrant = ("long_buildup" if dp > 0 and doi > 0 else
                "short_covering" if dp > 0 and doi < 0 else
                "short_buildup" if dp < 0 and doi > 0 else
                "long_unwinding" if dp < 0 and doi < 0 else "mixed")
    pcr_lean = ("put_heavy" if isinstance(pcr, float) and pcr > 1.25 else
                "call_heavy" if isinstance(pcr, float) and pcr < 0.80 else "neutral")
    # Confluence: does max pain / call wall / put floor fall inside a demand or supply zone?
    nd, ns = zones.get("nearest_demand"), zones.get("nearest_supply")
    mp_in_demand = bool(max_pain and nd and nd["low"] <= max_pain <= nd["high"])
    mp_in_supply = bool(max_pain and ns and ns["low"] <= max_pain <= ns["high"])
    cw_in_supply = bool(call_wall and ns and ns["low"] <= call_wall <= ns["high"])
    pf_in_demand = bool(put_floor and nd and nd["low"] <= put_floor <= nd["high"])
    return {
        "expiry": (d.get("expiries") or [None])[0],
        "pcr": pcr, "pcr_lean": pcr_lean, "max_pain": max_pain,
        "call_wall": call_wall, "put_floor": put_floor,
        "oi_quadrant": quadrant,
        "confluence": {
            "max_pain_at_demand": mp_in_demand,
            "max_pain_at_supply": mp_in_supply,
            "call_wall_confirms_supply": cw_in_supply,
            "put_floor_confirms_demand": pf_in_demand,
        },
        "note": ("PCR>1.25=put-heavy(protective,bullish tilt); <0.80=call-heavy(speculative,bearish tilt). "
                 "Max pain near a zone = gravitational confluence. Call wall confirms supply; put floor confirms demand."),
    }


def _build_fundamental_ctx(qres_data: dict, sub_sector: str | None) -> dict[str, Any]:
    """Compact fundamental digest for the Trader: earnings direction, bank KPIs, lean.
    This is NOT a full fundamental analysis — it calibrates whether the technical setup
    has fundamental tailwind or headwind."""
    if qres_data.get("status") != "ok":
        return {"status": "unavailable", "note": "fundamental data not available"}
    data = qres_data.get("data") or {}
    metrics = (data.get("metrics") or {}).get("metrics") or {}
    pat_m = metrics.get("pat") or {}
    eps_m = metrics.get("eps") or {}
    nm = (data.get("metrics") or {}).get("net_margin_pct") or {}
    latest_pat = pat_m.get("latest")
    pat_qoq = pat_m.get("qoq_1")
    pat_yoy = pat_m.get("yoy_1")
    pat_streak = pat_m.get("streak_quarters", 0)
    net_margin = nm.get("latest")
    margin_trend = ((nm.get("latest", 0) or 0) - (nm.get("year_ago", 0) or 0))
    # Bank KPIs from XBRL if available
    bank_kpis: dict[str, Any] = {}
    is_lender = bool(sub_sector and str(sub_sector).startswith(_LENDER_PREFIXES))
    if is_lender:
        filing = data.get("latest_filing") or {}
        xbrl = filing.get("xbrl") or {}
        k = xbrl.get("kpis") or {}
        if k.get("gross_npa_pct") is not None:
            bank_kpis = {
                "gnpa_pct": k.get("gross_npa_pct"), "nnpa_pct": k.get("net_npa_pct"),
                "nii_cr": k.get("nii"), "cet1_pct": k.get("cet1_pct"),
            }
    # Derive lean from PAT streak + direction
    if pat_streak >= 2 and (pat_yoy or 0) > 5:
        lean = "improving"
    elif pat_streak <= -2 or (pat_yoy is not None and pat_yoy < -10):
        lean = "deteriorating"
    else:
        lean = "stable"
    return {
        "latest_period": data.get("latest_period"),
        "series_source": data.get("series_source"),
        "latest_pat_cr": latest_pat,
        "pat_qoq_pct": pat_qoq, "pat_yoy_pct": pat_yoy, "pat_streak": pat_streak,
        "net_margin_pct": net_margin, "margin_trend_pct": round(margin_trend, 2),
        "bank_kpis": bank_kpis if bank_kpis else None,
        "fundamental_lean": lean,
        "note": ("Use as CALIBRATOR: an improving fundamental trend raises conviction for longs at demand; "
                 "deteriorating fundamentals cap the upside even if the zone is strong. "
                 "Do not repeat the full fundamental analysis here — that is the Fundamental agent's job."),
    }


# ──────────────────────── agent entry point ──────────────────────────────────

async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None) -> AgentResult:
    # Fetch all evidence concurrently (all tools are cached; no redundant API calls)
    import asyncio
    z_task       = tools.call("get_demand_supply_zones", symbol=symbol, as_of=as_of)
    events_task  = tools.call("nse_company_events", symbol=symbol, days=120, as_of=as_of)  # same window as Fundamental → shared cache
    deals_task   = tools.call("nse_deals", symbol=symbol, days=30, as_of=as_of)
    fii_task     = tools.call("nse_fii_dii", as_of=as_of)
    chain_task   = tools.call("get_option_chain", symbol=symbol, strikecount=10, as_of=as_of)
    qres_task    = tools.call("quarterly_fundamentals", symbol=symbol, as_of=as_of,
                              sub_sector=meta.get("sub_sector"))
    (z, events_data, deals_data, fii_dii,
     chain_data, qres_data) = await asyncio.gather(
        z_task, events_task, deals_task, fii_task, chain_task, qres_task)

    results = {"get_demand_supply_zones": z, "nse_company_events": events_data,
               "nse_deals": deals_data, "nse_fii_dii": fii_dii,
               "get_option_chain": chain_data, "quarterly_fundamentals": qres_data}

    if z["status"] != "ok":
        return unavailable_result(NAME, symbol, results, z.get("message") or "demand/supply zones unavailable")

    data = z["data"]
    as_of_dt = parse_as_of(as_of)

    event_ctx     = _build_event_context(events_data, deals_data, fii_dii, as_of_dt)
    option_ctx    = _build_option_ctx(chain_data, data)
    fundamental_ctx = _build_fundamental_ctx(qres_data, meta.get("sub_sector"))

    base, parts = baselines.trader_baseline(data, event_ctx=event_ctx,
                                            option_ctx=option_ctx, fundamental_ctx=fundamental_ctx)
    evidence = {
        "zones": data,
        "event_context": event_ctx,
        "option_snapshot": option_ctx,
        "fundamental_snapshot": fundamental_ctx,
    }
    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__),
                        evidence=evidence, tool_results=results,
                        context={**meta, "as_of": as_of}, baseline=base)
    if res.status == "ok":
        res.extra.update({
            "bull_score": parts.get("bull_score"), "bear_score": parts.get("bear_score"),
            "ema_stack": data.get("ema_stack"), "vwap_side": data.get("vwap_side"),
            "position": (data.get("price_action") or {}).get("position"),
            "nearest_demand": data.get("nearest_demand"), "nearest_supply": data.get("nearest_supply"),
            "volume_context": data.get("volume_context"),
            "mtf": data.get("mtf"), "trade_plan": data.get("trade_plan"),
            "long_plan": data.get("long_plan"), "short_plan": data.get("short_plan"),
            "option_snapshot": option_ctx, "fundamental_snapshot": fundamental_ctx,
            "event_context": event_ctx, "candle_provider": data.get("candle_provider"),
        })
        if data.get("vwap_session") is None:
            res.missing_data.append("session VWAP (no intraday volume)")
        if events_data.get("status") != "ok":
            res.missing_data.append("upcoming catalyst calendar (NSE events unavailable)")
        if chain_data.get("status") != "ok":
            res.missing_data.append("option chain (F&O data unavailable or non-F&O stock)")
        if qres_data.get("status") != "ok":
            res.missing_data.append("fundamental snapshot (quarterly results unavailable)")
    return res
