"""Supplementary fundamentals from Yahoo Finance. NSE filings (nse_company_events) are the primary authority."""
from __future__ import annotations

from typing import Any

from app.mcp_server.tools.common import clean_symbol, error, ok, unavailable, yahoo_symbol

_INFO_KEYS = ("longName", "marketCap", "trailingPE", "forwardPE", "priceToBook", "returnOnEquity", "returnOnAssets",
              "trailingEps", "dividendYield", "dividendRate", "payoutRatio", "debtToEquity", "earningsGrowth", "revenueGrowth",
              "profitMargins", "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "beta", "heldPercentInstitutions", "heldPercentInsiders", "currency")
_STMT_ROWS = ("Total Revenue", "Net Interest Income", "Interest Income", "Interest Expense", "Pretax Income", "Net Income",
              "Basic EPS", "Provision For Doubtful Accounts")


def yf_fundamentals(symbol: str) -> dict[str, Any]:
    """Valuation ratios, ROE, dividend data, last 6 quarters of revenue/NII/profit, 3y dividend history (Yahoo, unofficial).
    CURRENT snapshot only (not point-in-time). Banks' NIM/GNPA/NNPA are NOT here: read them from results filings."""
    src = "yfinance"
    try:
        import yfinance as yf

        tk = yf.Ticker(yahoo_symbol(symbol))
        info = tk.info or {}
        if not info or "longName" not in info:
            return unavailable(src, f"no Yahoo data for {clean_symbol(symbol)}")
        ratios = {k: info.get(k) for k in _INFO_KEYS if info.get(k) is not None}
        quarters: list[dict[str, Any]] = []
        qf = tk.quarterly_financials
        if qf is not None and not qf.empty:
            for col in list(qf.columns)[:6]:
                row = {"period_end": str(col.date())}
                for name in _STMT_ROWS:
                    if name in qf.index and qf.at[name, col] == qf.at[name, col]:
                        row[name] = float(qf.at[name, col])
                quarters.append(row)
        divs = tk.dividends
        div_hist = [] if divs is None or divs.empty else [{"date": str(i.date()), "amount": float(v)} for i, v in divs.tail(6).items()]
        return ok(src, {"ratios": ratios, "quarterly": quarters, "recent_dividends": div_hist},
                  symbol=clean_symbol(symbol), point_in_time=False,
                  note="Current snapshot; units as reported by Yahoo (INR). Cross-check with NSE results filings.")
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)


# =====================================================================================================
# Quarterly results history (up to 16 quarters) -> SQLite -> deterministic QoQ / YoY metrics
# =====================================================================================================
from datetime import date, timedelta  # noqa: E402

from app.mcp_server.tools import nse as nse_tools  # noqa: E402
from app.mcp_server.tools import xbrl  # noqa: E402
from app.mcp_server.tools.common import now_ist, parse_any_dt, parse_as_of  # noqa: E402
from app.service.settings import get_settings  # noqa: E402
from app.store import fundamentals as fq  # noqa: E402
from app.store.db import kv_get, kv_set, mark_refreshed, refreshed_today  # noqa: E402

RESULT_FRESH_DAYS = 70          # latest stored quarter younger than this -> do not fetch, use the stored data
RESULT_LAG_DAYS = 45            # when a filing date is unknown, assume the quarter was public 45 days after period end
NSE_UNIT_DIVISOR = 100.0        # NSE results API is in INR lakh -> crore. Cross-checked against Yahoo when both exist.
LENDER_PREFIXES = ("bank_", "nbfc", "housing_finance", "govt_financier")

_NSE_KEYS = {
    "period_end": ("toDate", "to_date", "periodEnded", "period_end"),
    "reported_on": ("re_broadcast_timestamp", "broadcastDate", "broadcast_date_time", "exchdisstime", "filingDate", "filing_date"),
    "revenue": ("income", "totalIncome", "reTotalIncome", "re_income", "totalIncomeFromOperations", "revenue", "reTotInc"),
    "pbt": ("reProLossBefTax", "proLossBefTax", "profitBeforeTax", "pbt", "re_pro_loss_bef_tax"),
    "pat": ("proLossAftTax", "reProLossAftTax", "netProfit", "pat", "re_pro_loss_aft_tax"),
    "eps": ("reDilEPS", "reBasicEPS", "basicEPS", "dilutedEPS", "eps", "dilEPS", "re_dil_eps"),
}
_NSE_ATTACH = ("xbrl_attachment", "xbrl", "naVal", "na_attachment", "attachment")
_NSE_DETAIL_KEYS = ("resultDetailedDataLink", "resultdetaileddatalink", "detailLink", "detailedDataLink")
NSE_XBRL_MAX = 12       # cap XBRL fetches per symbol per run (one per recent quarter)


def is_lender(sub_sector: str | None) -> bool:
    return bool(sub_sector) and str(sub_sector).startswith(LENDER_PREFIXES)


def _num(value: Any) -> float | None:
    if value in (None, "", "-", "NA"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    low = {k.lower(): v for k, v in row.items()}
    for k in keys:
        if k.lower() in low and low[k.lower()] not in (None, ""):
            return low[k.lower()]
    return None


def parse_nse_results(rows: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """NSE financial-results rows -> fundamentals_q rows (INR crore). Pure function (unit-tested with a fixture).
    NOTE: field names follow NSE's public JSON as last documented; verify on the first live run."""
    out = []
    for r in rows:
        end = parse_any_dt(_first(r, _NSE_KEYS["period_end"]))
        if end is None:
            continue
        reported = parse_any_dt(_first(r, _NSE_KEYS["reported_on"]))
        basis = "standalone" if str(r.get("consolidated", "")).lower().startswith(("non", "stand")) else "consolidated"
        conv = lambda k: (None if (v := _num(_first(r, _NSE_KEYS[k]))) is None else round(v / NSE_UNIT_DIVISOR, 2))  # noqa: E731
        row = {"symbol": symbol, "period_end": end.date().isoformat(), "basis": basis, "source": "nse", "op_profit": None, "op_kind": None,
               "reported_on": reported.date().isoformat() if reported else None,
               "revenue": conv("revenue"), "pbt": conv("pbt"), "pat": conv("pat"), "eps": _num(_first(r, _NSE_KEYS["eps"])),
               # Provenance (not stored in the series table; surfaced to the agent so it can cite the filing).
               "audited": (str(_first(r, ("audited", "audit")) or "").strip() or None),
               "xbrl_url": _first(r, _NSE_ATTACH)}
        if row["revenue"] is not None or row["pat"] is not None:
            out.append(row)
    return out


def parse_yahoo_statement(df: Any, symbol: str, lender: bool, sub_sector: str | None = None) -> list[dict[str, Any]]:
    """Yahoo quarterly income statement (columns = period end dates, INR) -> fundamentals_q rows in INR crore."""
    out = []
    if df is None or getattr(df, "empty", True):
        return out
    cr = 1e7

    def val(col, *names):
        for n in names:
            if n in df.index:
                v = df.at[n, col]
                if v == v and v is not None:
                    return float(v)
        return None

    for col in df.columns:
        rev = val(col, "Total Revenue", "Operating Revenue")
        pbt = val(col, "Pretax Income")
        pat = val(col, "Net Income", "Net Income Common Stockholders")
        if lender:
            prov = val(col, "Provision For Doubtful Accounts", "Provision for Loan Losses", "Credit Losses Provision")
            op, kind = ((pbt + prov, "PPOP~") if pbt is not None and prov is not None else (None, None))
        elif sub_sector and sub_sector.startswith(("insurance", "holding")):
            op, kind = None, None
        else:
            op, kind = val(col, "EBITDA", "Normalized EBITDA"), "EBITDA"
        out.append({"symbol": symbol, "period_end": col.date().isoformat(), "basis": "consolidated", "source": "yahoo",
                    "revenue": None if rev is None else round(rev / cr, 2), "op_profit": None if op is None else round(op / cr, 2),
                    "op_kind": kind if op is not None else None, "pbt": None if pbt is None else round(pbt / cr, 2),
                    "pat": None if pat is None else round(pat / cr, 2), "eps": val(col, "Basic EPS", "Diluted EPS"), "reported_on": None})
    return out


def merge_op_profit(nse_rows: list[dict[str, Any]], yahoo_rows: list[dict[str, Any]]) -> list[str]:
    """NSE summary rows carry no EBITDA / PPOP: borrow Yahoo's for the same quarter (tagged '(yahoo)') and warn when the
    two sources disagree on revenue by more than 5x (a sign of a unit problem)."""
    notes = []
    by_end = {r["period_end"]: r for r in yahoo_rows}
    for r in nse_rows:
        y = by_end.get(r["period_end"])
        if not y:
            continue
        if r["op_profit"] is None and y.get("op_profit") is not None:
            r["op_profit"], r["op_kind"] = y["op_profit"], f"{y['op_kind']}(yahoo)"
        if r.get("revenue") and y.get("revenue") and not 0.2 <= r["revenue"] / y["revenue"] <= 5:
            notes.append(f"unit/definition mismatch {r['period_end']}: NSE revenue {r['revenue']} vs Yahoo {y['revenue']} (INR cr). Check NSE_UNIT_DIVISOR")
    return notes


def _abs_nse_url(link: Any) -> str | None:
    if not link or not isinstance(link, str):
        return None
    if link.startswith("http"):
        return link
    return f"{nse_tools.BASE}{link if link.startswith('/') else '/' + link}"


def nse_filings(raw: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """Parse the financial-results LIST (metadata only — NO numbers) into filing records: period_end, basis,
    reported_on, audited, and the per-filing `xbrl_url` + `detail_link` that actually carry the figures."""
    out = []
    for r in raw:
        end = parse_any_dt(_first(r, _NSE_KEYS["period_end"]))
        if end is None:
            continue
        rep = parse_any_dt(_first(r, _NSE_KEYS["reported_on"]))
        basis = "standalone" if str(r.get("consolidated", "")).lower().startswith(("non", "stand")) else "consolidated"
        out.append({"symbol": symbol, "period_end": end.date().isoformat(), "basis": basis,
                    "reported_on": rep.date().isoformat() if rep else None,
                    "audited": (str(_first(r, ("audited", "audit")) or "").strip() or None),
                    "xbrl_url": _abs_nse_url(_first(r, _NSE_ATTACH)),
                    "detail_link": _abs_nse_url(_first(r, _NSE_DETAIL_KEYS))})
    return out


def _detail_rows(d: Any) -> list[dict[str, Any]]:
    """Normalise a resultDetailedDataLink response (list, or dict possibly wrapping data/resultBody/rows) to row dicts."""
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    if isinstance(d, dict):
        inner = d.get("data") or d.get("resultBody") or d.get("rows")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        if isinstance(inner, dict):
            return [inner]
        return [d]
    return []


def _fetch_nse_results(sym: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # The LIST endpoint returns filing metadata + per-filing links. The numbers live in each filing's XBRL (the
    # summary "detail link" endpoint is bot-blocked), so we build the whole series from the XBRL: P&L + bank KPIs.
    ref = f"{nse_tools.BASE}/get-quote/equity/{sym}"
    params = {"index": "equities", "symbol": sym, "period": "Quarterly"}
    payload = nse_tools.nse_get("/api/corporates-financial-results", params, referer=ref)
    raw = nse_tools._rows(payload)
    filings = nse_filings(raw, sym)
    by_recent = sorted(filings, key=lambda f: f["period_end"], reverse=True)
    cons = [f for f in by_recent if f["basis"] == "consolidated" and f.get("xbrl_url")]
    targets = (cons or [f for f in by_recent if f.get("xbrl_url")])[:NSE_XBRL_MAX]

    rows: list[dict[str, Any]] = []
    xd = {"fetched": 0, "parsed_rows": 0, "latest_status": None, "sample_tags": [], "errors": []}
    latest_xbrl: dict[str, Any] | None = None
    for i, f in enumerate(targets):
        xb = xbrl.fetch_bank_kpis(f["xbrl_url"], referer=ref)
        if i == 0:
            latest_xbrl, xd["latest_status"], xd["sample_tags"] = xb, xb.get("status"), (xb.get("sample_tags") or [])
        if xb.get("status") in ("ok", "partial"):
            xd["fetched"] += 1
            k = xb.get("kpis") or {}
            if k.get("pat") is not None or k.get("total_income") is not None:   # a usable P&L row (values already in crore)
                rows.append({"symbol": sym, "period_end": f["period_end"], "basis": f["basis"], "source": "nse",
                             "revenue": k.get("total_income"), "op_profit": k.get("op_profit"),
                             "op_kind": "PPOP" if k.get("op_profit") is not None else None,
                             "pbt": k.get("pbt"), "pat": k.get("pat"), "eps": k.get("eps"), "reported_on": f.get("reported_on")})
        else:
            xd["errors"].append(f"{f['period_end']}: {xb.get('error') or xb.get('status')}")
    xd["parsed_rows"] = len(rows)

    latest = cons[0] if cons else (by_recent[0] if by_recent else None)
    lf = None
    if latest:
        lf = {k: latest.get(k) for k in ("period_end", "basis", "reported_on", "audited", "xbrl_url")}
        if latest_xbrl is not None:
            lf["xbrl"] = latest_xbrl   # already fetched; the caller must not re-fetch
    # Why do recent quarters get skipped? Dump the NEWEST filings' raw fields so it is visible in the report:
    # whether the 2025/2026 rows carry an `xbrl` link, a detail link, and how they classify.
    def _rawdate(r: dict[str, Any]) -> str:
        d = parse_any_dt(_first(r, _NSE_KEYS["period_end"]))
        return d.date().isoformat() if d else ""
    recent_raw = [{k: r.get(k) for k in ("toDate", "consolidated", "audited", "xbrl", "resultDetailedDataLink", "format", "indAs", "reInd", "oldNewFlag")}
                  for r in sorted(raw, key=_rawdate, reverse=True)[:6]]
    diag = {"endpoint": "/api/corporates-financial-results", "payload_type": type(payload).__name__,
            "list_rows": len(raw), "filings_found": len(filings), "parsed_rows": len(rows),
            "newest_filing_any": (by_recent[0]["period_end"] if by_recent else None),
            "newest_consolidated_with_xbrl": (cons[0]["period_end"] if cons else None),
            "filings_with_xbrl": sum(1 for f in filings if f.get("xbrl_url")),
            "sample_raw_keys": (sorted(raw[0].keys())[:30] if raw else []),
            "recent_raw": recent_raw, "xbrl": xd, "latest_filing": lf}
    return rows, diag


def _row_completeness(r: dict[str, Any]) -> int:
    return sum(r.get(k) is not None for k in ("pat", "eps", "pbt", "revenue", "op_profit"))


def _fetch_yahoo_results(sym: str, sub_sector: str | None) -> list[dict[str, Any]]:
    import yfinance as yf

    tk = yf.Ticker(yahoo_symbol(sym))
    lender = is_lender(sub_sector)
    # Yahoo exposes TWO quarterly income statements whose coverage differs: one often carries the most recently
    # reported quarter the other still lacks. Merge both so the series is not a quarter behind, keeping the more
    # complete row per period. (Both are source="yahoo", so pick_series stays single-source.)
    by_end: dict[str, dict[str, Any]] = {}
    for attr in ("quarterly_income_stmt", "quarterly_financials"):
        df = getattr(tk, attr, None)
        for r in parse_yahoo_statement(df, sym, lender, sub_sector):
            cur = by_end.get(r["period_end"])
            if cur is None or _row_completeness(r) > _row_completeness(cur):
                by_end[r["period_end"]] = r
    return sorted(by_end.values(), key=lambda r: r["period_end"])


def _public_on(row: dict[str, Any]) -> date:
    if row.get("reported_on"):
        return date.fromisoformat(row["reported_on"])
    return date.fromisoformat(row["period_end"]) + timedelta(days=RESULT_LAG_DAYS)


def quarterly_fundamentals(symbol: str, as_of: str | None = None, sub_sector: str | None = None) -> dict[str, Any]:
    """Up to 16 quarters of revenue, operating profit (EBITDA, or PPOP for lenders), PBT, PAT, EPS from NSE results filings
    (Yahoo as fallback), kept in SQLite, plus Python-computed QoQ (last 6) / YoY (last 4) changes and streaks.
    Re-fetches only when the newest stored quarter is >= 70 days old and nothing was fetched today."""
    src = "nse+yahoo (SQLite cache)"
    sym = clean_symbol(symbol)
    try:
        t = parse_as_of(as_of)
        today = now_ist().date()
        live = t.date() >= today
        latest = fq.latest_period_end(sym, today)
        age = (today - latest).days if latest else None
        errors: list[str] = []
        cache = "hit"
        filing: dict[str, Any] | None = None
        if live and (latest is None or age >= RESULT_FRESH_DAYS or get_settings().always_fresh or get_settings().force_refresh) and not refreshed_today("results", sym):
            cache, nse_rows, yahoo_rows, nse_diag = "refreshed", [], [], {}
            try:
                nse_rows, nse_diag = _fetch_nse_results(sym)
            except Exception as exc:  # noqa: BLE001 - one source failing must not hide the other
                nse_diag = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                errors.append(f"nse: {nse_diag['error']}")
            try:
                yahoo_rows = _fetch_yahoo_results(sym, sub_sector)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"yahoo: {type(exc).__name__}: {str(exc)[:160]}")
            errors.extend(merge_op_profit(nse_rows, yahoo_rows))
            # Provenance comes from the LIST metadata (it carries the xbrl_url); fall back to the newest parsed row.
            filing = nse_diag.get("latest_filing")
            if filing is None and nse_rows:
                newest = max(nse_rows, key=lambda r: r["period_end"])
                filing = {"period_end": newest["period_end"], "reported_on": newest.get("reported_on"),
                          "audited": newest.get("audited"), "basis": newest["basis"], "xbrl_url": newest.get("xbrl_url")}
            if filing and is_lender(sub_sector) and filing.get("xbrl_url") and "xbrl" not in filing:
                # KPIs live in the XBRL; _fetch_nse_results usually already attached the newest one, so only
                # fetch here if it didn't. Failures are captured in filing["xbrl"], never raised.
                filing = dict(filing)
                filing["xbrl"] = xbrl.fetch_bank_kpis(filing["xbrl_url"], referer=f"{nse_tools.BASE}/get-quote/equity/{sym}")
            if filing and filing.get("xbrl") and filing["xbrl"].get("status") not in ("ok", "partial"):
                errors.append(f"xbrl: {filing['xbrl'].get('status')} ({filing['xbrl'].get('error') or 'no KPIs matched'})")
            if nse_rows or yahoo_rows:
                fq.upsert_quarters(nse_rows + yahoo_rows)
                mark_refreshed("results", sym)      # a day with zero rows is retried on the next load
            # Persist the fetch outcome so it is visible in the report even when a later call is a cache-hit
            # (the agent's own call usually hits cache after the pre-run refresh job did the real fetch).
            kv_set("results_fetch", sym, {"fetched_at": now_ist().isoformat(timespec="seconds"), "nse": nse_diag,
                                          "yahoo_rows": len(yahoo_rows), "source_errors": errors, "latest_filing": filing})
        rows, used, basis = fq.pick_series(sym, t.date())
        # Surface the last fetch's diagnostics + what is actually stored, on every call (incl. cache-hits).
        last_fetch = kv_get("results_fetch", sym)
        if filing is None and last_fetch:
            filing = last_fetch.get("latest_filing")
        if not errors and last_fetch:
            errors = last_fetch.get("source_errors") or []
        if not live:                      # backtest: keep only quarters that were public by T (live runs just fetched what is public now)
            rows = [r for r in rows if _public_on(r) <= t.date()]
        if not rows:
            return unavailable(src, f"no stored quarterly results for {sym}" + (f" ({'; '.join(errors)})" if errors else "")
                               + ". NSE/Yahoo were not reachable: load your own history with `python -m app.jobs.import_results file.csv`.",
                               cache=cache, source_errors=errors, last_fetch=last_fetch)
        slim = [{k: r.get(k) for k in ("period_end", "revenue", "op_profit", "op_kind", "pbt", "pat", "eps", "reported_on")} for r in rows]
        # Honest label: only describe the operating-profit line when the series actually carries op_profit values.
        present_kinds = sorted({r.get("op_kind") for r in rows if r.get("op_profit") is not None and r.get("op_kind")})
        if present_kinds:
            op_kind_label = " / ".join(present_kinds)
        else:
            expected = "PPOP (approx.)" if is_lender(sub_sector) else "EBITDA"
            op_kind_label = f"unavailable (no {expected} in this series: NSE summary has no operating-profit line and no Yahoo match)"
        return ok(src, {"series_source": used, "basis": basis, "unit": "INR crore (EPS in INR)", "quarters": slim,
                        "latest_filing": filing, "source_counts": fq.source_counts(sym), "last_fetch": last_fetch,
                        "metrics": fq.compute_metrics(rows)},
                  symbol=sym, cache=cache, latest_period=rows[-1]["period_end"], age_days=(today - date.fromisoformat(rows[-1]["period_end"])).days,
                  source_errors=errors, operating_profit_kind=op_kind_label)
    except Exception as exc:  # noqa: BLE001
        return error(src, exc)
