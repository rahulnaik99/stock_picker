"""Fundamental Analyst: results, dividends, buybacks, splits/bonus, board decisions, filings, upcoming catalysts."""
from __future__ import annotations

from typing import Any

from app.Agents.base import analyze, compact, load_prompt, unavailable_result
from app.ledger.events import EventLedger
from app.mcp_server.client import AgentTools
from app.mcp_server.tools.common import parse_as_of
from app.schemas import AgentResult
from app.scoring import baselines, validators

NAME = "fundamental"
TOOLS = ["nse_company_events", "quarterly_fundamentals", "yf_fundamentals", "news_search"]


def news_query(company: str) -> str:
    return f'"{company}" results OR dividend OR board OR RBI'


def _ratios_only(fund: dict[str, Any]) -> dict[str, Any]:
    """Valuation ratios and dividends only: quarterly numbers come from the stored history (quarterly_fundamentals)."""
    if fund.get("status") != "ok":
        return fund
    d = fund["data"]
    return {**{k: v for k, v in fund.items() if k != "data"}, "data": {"ratios": d.get("ratios"), "recent_dividends": d.get("recent_dividends")}}


async def run(symbol: str, meta: dict[str, Any], tools: AgentTools, as_of: str | None, ledger: EventLedger | None = None) -> AgentResult:
    ledger = ledger or EventLedger()
    company = meta.get("company_name") or symbol
    nse = await tools.call("nse_company_events", symbol=symbol, days=120, as_of=as_of)
    qres = await tools.call("quarterly_fundamentals", symbol=symbol, as_of=as_of, sub_sector=meta.get("sub_sector"))
    fund = await tools.call("yf_fundamentals", symbol=symbol)
    news = await tools.call("news_search", query=news_query(company), days=14, as_of=as_of)
    results = {"nse_company_events": nse, "quarterly_fundamentals": qres, "yf_fundamentals": fund, "news_search": news}

    # Persist fresh events, then read back through the ledger so history + point-in-time rules apply.
    if nse["status"] == "ok":
        ledger.upsert(nse["data"])
    events = ledger.query(symbol, parse_as_of(as_of))
    if not events and qres["status"] != "ok" and fund["status"] != "ok" and news["status"] != "ok":
        return unavailable_result(NAME, symbol, results, "No NSE events, quarterly results, fundamentals or news could be retrieved.")

    # Guard against mislabelling: if a results filing is newer than the latest quarter in the numeric series,
    # tell the model that quarter's actuals are missing (prevents attaching Jun figures to a just-filed Sep quarter).
    freshness = validators.results_freshness(events, qres.get("latest_period") if qres["status"] == "ok" else None)

    evidence = {
        # Cap to the most recent events and drop the long source_url/published_at (kept in the ledger for the UI);
        # the model cites event_id, not the URL. This keeps the prompt within small local-model context windows.
        "events_ledger": [{k: e[k] for k in ("event_id", "event_type", "event_status", "event_date", "event_summary")}
                          for e in events[:15]],
        "quarterly_results": qres if qres["status"] != "ok" else {**qres["data"], "operating_profit_kind": qres.get("operating_profit_kind"),
                                                                   "latest_period": qres.get("latest_period"), "source_errors": qres.get("source_errors")},
        "yf_fundamentals": _ratios_only(fund), "news": compact(news, 6),
        "tool_status": {k: v["status"] for k, v in results.items()},
    }
    if freshness:
        evidence["data_freshness"] = {"warning": freshness}
    metrics_block = (qres.get("data") or {}).get("metrics") if qres["status"] == "ok" else None
    base_score, _parts = baselines.fundamental_baseline(metrics_block, meta.get("sub_sector"))
    res = await analyze(agent=NAME, llm_key=NAME, symbol=symbol, system_prompt=load_prompt(__file__, sub_sector=meta.get("sub_sector", "")),
                        evidence=evidence, tool_results=results, context={**meta, "as_of": as_of}, baseline=base_score)
    if res.status == "ok":
        if freshness:
            res.extra["data_freshness_flag"] = freshness
            res.missing_data.append("latest filed quarter not yet in the numeric series (see data_freshness)")
        ledger.update_impacts((res.extra or {}).get("events", []) or [])
        if nse["status"] != "ok":
            res.missing_data.append("nse_company_events (official filings not retrieved)")
        if qres["status"] != "ok":
            res.missing_data.append("quarterly results history (NSE/Yahoo not retrieved)")
        elif qres["data"]["metrics"].get("history_short"):
            res.missing_data.append(f"only {qres['data']['metrics']['quarters_available']} of 16 quarters of results available")
        res.extra["results_metrics"] = (qres.get("data") or {}).get("metrics", {}).get("metrics")
    return res
