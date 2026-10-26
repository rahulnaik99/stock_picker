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


_POS_WORDS = ("profit up", "profit rises", "surge", "jump", "record", "beats", "upgrade", "strong", "growth",
              "rally", "gain", "high", "buy", "outperform", "raises", "robust")
_NEG_WORDS = ("profit fall", "falls", "drop", "decline", "loss", "downgrade", "fraud", "penalty", "probe",
              "cut", "weak", "slump", "sell", "miss", "warning", "lawsuit", "default", "stake sale")
_ACTION_TYPES = ("dividend", "buyback", "split_bonus", "fundraise", "merger_acquisition", "credit_rating")


def _news_lean(news: dict[str, Any]) -> dict[str, Any]:
    """Naive +/- tally over headline text (unverified sentiment, context only)."""
    items = (news.get("data") or []) if news.get("status") == "ok" else []
    pos = sum(any(w in (it.get("title") or "").lower() for w in _POS_WORDS) for it in items)
    neg = sum(any(w in (it.get("title") or "").lower() for w in _NEG_WORDS) for it in items)
    lean = "positive" if pos > neg else "negative" if neg > pos else "neutral"
    return {"headlines": len(items), "positive": pos, "negative": neg, "lean": lean,
            "note": "headline keyword tally; unverified, context only"}


def _forward_view(events: list[dict[str, Any]], as_of: str | None, fund: dict[str, Any], news: dict[str, Any]) -> dict[str, Any]:
    """Forward-looking digest: scheduled catalysts, recent dividends and corporate actions, plus the news lean."""
    from datetime import date as _date
    today = parse_as_of(as_of).date() if as_of else _date.today()
    upcoming = [{"what": e.get("event_summary", "")[:90], "date": e.get("event_date"), "type": e.get("event_type")}
                for e in events if e.get("event_status") == "scheduled" and (e.get("event_date") or "") >= today.isoformat()][:5]
    actions = [{"type": e.get("event_type"), "date": e.get("event_date"), "summary": e.get("event_summary", "")[:90]}
               for e in events if e.get("event_type") in _ACTION_TYPES][:6]
    divs = (fund.get("data") or {}).get("recent_dividends") if fund.get("status") == "ok" else None
    return {"upcoming_catalysts": upcoming, "recent_dividends": (divs or [])[-4:],
            "corporate_actions": actions, "news_lean": _news_lean(news)}


def _ratios_only(fund: dict[str, Any]) -> dict[str, Any]:
    """Valuation ratios and dividends only: quarterly numbers come from the stored history (quarterly_fundamentals)."""
    if fund.get("status") != "ok":
        return fund
    d = fund["data"]
    return {**{k: v for k, v in fund.items() if k != "data"}, "data": {"ratios": d.get("ratios"), "recent_dividends": d.get("recent_dividends")}}


def _bank_kpis_yahoo(fund: dict[str, Any]) -> dict[str, Any] | None:
    """Recent NII / net income / EPS from Yahoo's quarterly statement (INR crore, unofficial). This is the one place
    NII is available without the results PDFs; keep it as context, cross-checked against filings."""
    if fund.get("status") != "ok":
        return None
    cr = 1e7
    rows = []
    for q in ((fund.get("data") or {}).get("quarterly") or [])[:4]:
        nii, ni = q.get("Net Interest Income"), q.get("Net Income")
        if nii is None and ni is None:
            continue
        rows.append({"period_end": q.get("period_end"),
                     "nii_cr": round(nii / cr, 2) if nii is not None else None,
                     "net_income_cr": round(ni / cr, 2) if ni is not None else None,
                     "eps": q.get("Basic EPS")})
    if not rows:
        return None
    return {"source": "yahoo (unofficial, cross-check filings)", "unit": "INR crore", "quarters": rows}


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

    # Quarterly results evidence. Keep the FULL tool response (incl. XBRL sample_tags) in `results` for the debug
    # report, but send the LLM a slim view: compact bank KPIs + a filing without the heavy tag dump.
    qr: dict[str, Any] = qres
    bank_kpis_nse = None
    if qres["status"] == "ok":
        data = qres["data"]
        qr = {**data, "operating_profit_kind": qres.get("operating_profit_kind"),
              "latest_period": qres.get("latest_period"), "source_errors": qres.get("source_errors")}
        qr.pop("last_fetch", None)          # fetch diagnostics stay in the raw tool trace (report), not the LLM prompt
        qr.pop("source_counts", None)
        filing = data.get("latest_filing")
        if filing:
            xd = filing.get("xbrl")
            if xd:   # always surface the XBRL outcome (incl. failures) so the report is self-explanatory
                bank_kpis_nse = {"status": xd.get("status"), "kpis": xd.get("kpis") or {}, "missing": xd.get("missing") or [],
                                 "error": xd.get("error"), "source": "NSE results XBRL (authoritative)"}
            qr["latest_filing"] = {k: filing[k] for k in ("period_end", "reported_on", "audited", "basis", "xbrl_url") if k in filing}

    evidence = {
        # Cap to the most recent events and drop the long source_url/published_at (kept in the ledger for the UI);
        # the model cites event_id, not the URL. This keeps the prompt within small local-model context windows.
        "events_ledger": [{k: e[k] for k in ("event_id", "event_type", "event_status", "event_date", "event_summary")}
                          for e in events[:15]],
        "quarterly_results": qr,
        "yf_fundamentals": _ratios_only(fund), "news": compact(news, 6),
        "tool_status": {k: v["status"] for k, v in results.items()},
    }
    if bank_kpis_nse:
        evidence["bank_kpis_nse"] = bank_kpis_nse
    kpis = _bank_kpis_yahoo(fund)
    if kpis:
        evidence["bank_kpis_yahoo"] = kpis
    # Three-horizon view: long-term (3 fiscal years), short-term (last 3 quarters), forward (catalysts/actions/news).
    from app.store import fundamentals as fq
    quarters = (qres.get("data") or {}).get("quarters") if qres["status"] == "ok" else None
    horizons = fq.horizon_metrics(quarters or [])
    evidence["fundamental_view"] = {"long_term_3y": horizons["long_term_3y"], "short_term_3q": horizons["short_term_3q"],
                                    "forward": _forward_view(events, as_of, fund, news)}
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
        res.extra["fundamental_view"] = evidence.get("fundamental_view")
    return res
