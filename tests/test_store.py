"""SQLite-backed datasets: 70-day results rule, news/event dedupe, FII/DII analytics, option-chain levels, multi-timeframe
indicators, shared industry score, run storage and refresh-on-load. All offline."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.graph import runner
from app.jobs import refresh as refresh_job
from app.mcp_server import registry
from app.mcp_server.tools import fundamentals, indicators, market, news, nse
from app.mcp_server.tools.common import IST, now_ist
from app.service.settings import get_settings
from app.store import events as event_store
from app.store import flows
from app.store import fundamentals as fq
from app.store import news as news_store
from app.store import runs as run_store
from tests.fakes import fake_tools
from tests.test_tools import CHAIN, trend_frame

TODAY = now_ist().date()


def qrow(sym: str, days_ago: int, rev: float, source: str = "nse", **kw):
    return {"symbol": sym, "period_end": (TODAY - timedelta(days=days_ago)).isoformat(), "basis": "consolidated", "source": source,
            "revenue": rev, "op_profit": rev * 0.4, "op_kind": "PPOP~", "pbt": rev * 0.3, "pat": rev * 0.2, "eps": 1.0, "reported_on": None, **kw}


# ---------------------------------------------------------------- results: parsing + metrics
def test_parse_nse_results_converts_lakh_to_crore_and_basis():
    rows = fundamentals.parse_nse_results([
        {"toDate": "30-Sep-2026", "broadcastDate": "15-Oct-2026 10:00:00", "income": "3166810", "reProLossBefTax": "500000",
         "proLossAftTax": "372000", "reDilEPS": "41.2", "consolidated": "Consolidated"},
        {"toDate": "30-Jun-2026", "income": "3000000", "proLossAftTax": "350000", "consolidated": "Non-Consolidated"},
        {"income": "1"}], "SBIN")
    assert len(rows) == 2 and rows[0]["revenue"] == 31668.1 and rows[0]["pat"] == 3720.0 and rows[0]["reported_on"] == "2026-10-15"
    assert rows[0]["basis"] == "consolidated" and rows[1]["basis"] == "standalone" and rows[0]["source"] == "nse"


def test_parse_nse_results_real_schema_and_provenance():
    # Real NSE corporates-financial-results keys + the actual CANBK Sep-2026 consolidated filing (INR lakh in, crore out).
    rows = fundamentals.parse_nse_results([
        {"to_date": "30-Sep-2026", "re_broadcast_timestamp": "14-Oct-2026 17:40", "income": "4093880.00",
         "reProLossBefTax": "698562.00", "proLossAftTax": "518203.00", "reDilEPS": "5.93", "consolidated": "Consolidated",
         "audited": "Un-Audited", "xbrl_attachment": "https://nsearchives.nseindia.com/corporate/xbrl/CANBK.xml"}], "CANBK")
    r = rows[0]
    assert r["revenue"] == 40938.8 and r["pbt"] == 6985.62 and r["pat"] == 5182.03 and r["eps"] == 5.93
    assert r["basis"] == "consolidated" and r["reported_on"] == "2026-10-14"
    assert r["audited"] == "Un-Audited" and r["xbrl_url"].endswith("CANBK.xml")


def test_parse_yahoo_statement_lender_ppop_and_ebitda():
    cols = [pd.Timestamp("2026-09-30"), pd.Timestamp("2026-06-30")]
    df = pd.DataFrame({cols[0]: [3.1e11, 1.0e11, 7e10, 2e11, 41.0, 2e10], cols[1]: [3.0e11, 0.9e11, 6e10, 1.9e11, 38.0, 1e10]},
                      index=["Total Revenue", "Pretax Income", "Net Income", "EBITDA", "Basic EPS", "Provision For Doubtful Accounts"])
    bank = fundamentals.parse_yahoo_statement(df, "SBIN", lender=True)
    assert bank[0]["revenue"] == 31000.0 and bank[0]["op_kind"] == "PPOP~" and bank[0]["op_profit"] == 12000.0   # (pbt + provision) / 1e7
    corp = fundamentals.parse_yahoo_statement(df, "BSE", lender=False)
    assert corp[0]["op_kind"] == "EBITDA" and corp[0]["op_profit"] == 20000.0
    assert fundamentals.parse_yahoo_statement(df, "LICI", lender=False, sub_sector="insurance_life")[0]["op_profit"] is None


def test_merge_op_profit_borrows_from_yahoo_and_flags_unit_mismatch():
    nse_rows = [{"period_end": "2026-09-30", "revenue": 100.0, "op_profit": None, "op_kind": None},
                {"period_end": "2026-06-30", "revenue": 100000.0, "op_profit": None, "op_kind": None}]
    yahoo_rows = [{"period_end": "2026-09-30", "revenue": 105.0, "op_profit": 40.0, "op_kind": "EBITDA"},
                  {"period_end": "2026-06-30", "revenue": 100.0, "op_profit": 38.0, "op_kind": "EBITDA"}]
    notes = fundamentals.merge_op_profit(nse_rows, yahoo_rows)
    assert nse_rows[0]["op_profit"] == 40.0 and nse_rows[0]["op_kind"] == "EBITDA(yahoo)" and len(notes) == 1 and "2026-06-30" in notes[0]


def test_fetch_yahoo_results_merges_both_statements(monkeypatch):
    import yfinance as yf
    # quarterly_income_stmt lags (no Sep-30); quarterly_financials carries the newer quarter. Merge must include both.
    income = pd.DataFrame({pd.Timestamp("2026-06-30"): [1.7e12, 6.5e10, 5.18e10, 5.71]},
                          index=["Total Revenue", "Pretax Income", "Net Income", "Basic EPS"])
    fin = pd.DataFrame({pd.Timestamp("2026-09-30"): [1.67e12, 6.98e10, 5.38e10, 5.93], pd.Timestamp("2026-06-30"): [1.7e12, 6.5e10, 5.18e10, 5.71]},
                       index=["Total Revenue", "Pretax Income", "Net Income", "Basic EPS"])

    class FakeTk:
        quarterly_income_stmt = income
        quarterly_financials = fin
    monkeypatch.setattr(yf, "Ticker", lambda s: FakeTk())
    rows = fundamentals._fetch_yahoo_results("SBIN", "bank_psu")
    ends = [r["period_end"] for r in rows]
    assert "2026-09-30" in ends and "2026-06-30" in ends
    assert next(r for r in rows if r["period_end"] == "2026-09-30")["pat"] == 5380.0


def test_bank_kpis_yahoo_surfaces_nii():
    from app.Agents.Fundamental import agent as fa
    fund = {"status": "ok", "data": {"quarterly": [{"period_end": "2026-09-30", "Net Interest Income": 1.1e11, "Net Income": 5.38e10, "Basic EPS": 5.93}]}}
    k = fa._bank_kpis_yahoo(fund)
    assert k["quarters"][0]["nii_cr"] == 11000.0 and k["quarters"][0]["net_income_cr"] == 5380.0
    assert fa._bank_kpis_yahoo({"status": "unavailable"}) is None


def test_compute_metrics_qoq_yoy_streak_and_gaps():
    rows = [{"period_end": (datetime(2025, 3, 31) + timedelta(days=91 * i)).date().isoformat(), "revenue": r, "op_profit": None, "pbt": None, "pat": p, "eps": None}
            for i, (r, p) in enumerate([(100, 10), (105, 11), (110, 9), (120, 12), (132, 15), (121, 16)])]
    m = fq.compute_metrics(rows)
    rev, pat = m["metrics"]["revenue"], m["metrics"]["pat"]
    assert rev["qoq_1"] == -8.33 and rev["qoq_2"] == 10.0 and rev["yoy_1"] == 15.24 and rev["streak_quarters"] == -1
    assert pat["streak_quarters"] == 3 and m["history_short"] and m["quarters_available"] == 6 and "op_profit" not in m["metrics"]


def test_compute_metrics_aligns_by_calendar_quarter_when_a_quarter_is_missing():
    # CANBK-style series with the Jul-Sep 2025 quarter missing: comparisons must align by calendar, not row position.
    series = [("2025-03-31", 18700.23, 5070.19), ("2025-06-30", 19449.35, 3194.95), ("2025-12-31", 10380.57, 5253.67),
              ("2026-03-31", 14514.26, 4574.23), ("2026-06-30", 16958.31, 5180.71)]
    rows = [{"period_end": pe, "revenue": rev, "op_profit": None, "pbt": None, "pat": pat, "eps": None} for pe, rev, pat in series]
    m = fq.compute_metrics(rows)
    rev, pat = m["metrics"]["revenue"], m["metrics"]["pat"]
    # YoY: Jun-26 vs Jun-25 (the SAME quarter a year earlier), not vs Mar-25.
    assert pat["yoy_1"] == 62.15 and rev["yoy_1"] == -12.81
    # QoQ that would span the hole is null, with a reason naming the missing quarter.
    assert pat["qoq_1"] == 13.26 and pat["qoq_2"] == -12.93 and pat["qoq_3"] is None
    assert any("Jul-Sep" in r for r in pat["null_reasons"])
    # net margin year-ago is the same quarter a year back (Jun-25), not rows[-5] (Mar-25).
    assert m["net_margin_pct"]["latest"] == 30.55 and m["net_margin_pct"]["year_ago"] == 16.43


# ---------------------------------------------------------------- results: the 70-day rule + refresh-once
def _no_fetch(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network fetch must not happen")
    monkeypatch.setattr(fundamentals, "_fetch_nse_results", boom)
    monkeypatch.setattr(fundamentals, "_fetch_yahoo_results", boom)


def test_results_younger_than_70_days_use_stored_data(monkeypatch):
    monkeypatch.setattr(get_settings(), "always_fresh", False)  # this test exercises the 70-day gate (always_fresh overrides it)
    fq.upsert_quarters([qrow("SBIN", 400 - 91 * i, 100 + i) for i in range(5)] + [qrow("SBIN", 40, 120)])
    _no_fetch(monkeypatch)
    out = fundamentals.quarterly_fundamentals("SBIN", sub_sector="bank_psu")
    assert out["status"] == "ok" and out["cache"] == "hit" and out["data"]["metrics"]["quarters_available"] == 6


def test_always_fresh_refetches_even_when_under_70_days(monkeypatch):
    fq.upsert_quarters([qrow("SBIN", 40, 120)])          # data is fresh (<70 days)
    calls = []
    monkeypatch.setattr(fundamentals, "_fetch_nse_results", lambda sym: (calls.append("nse") or [qrow(sym, 35, 121)], {}))
    monkeypatch.setattr(fundamentals, "_fetch_yahoo_results", lambda sym, sub: calls.append("yahoo") or [])
    out = fundamentals.quarterly_fundamentals("SBIN")     # always_fresh default True -> refetch anyway
    assert out["cache"] == "refreshed" and "nse" in calls


def test_reset_daily_caches_clears_gate_and_volatile_kv():
    from app.store.db import kv_get, kv_set, mark_refreshed, refreshed_today, reset_daily_caches
    mark_refreshed("results", "SBIN")
    kv_set("option_chain", "SBIN|d", {"a": 1})
    kv_set("industry_score", "2026-10-25:v2", {"s": 1})
    assert refreshed_today("results", "SBIN")
    reset_daily_caches()
    assert not refreshed_today("results", "SBIN")
    assert kv_get("option_chain", "SBIN|d") is None and kv_get("industry_score", "2026-10-25:v2") is None


def test_results_older_than_70_days_fetch_once_then_gate(monkeypatch):
    fq.upsert_quarters([qrow("SBIN", 120, 100)])
    calls = []
    monkeypatch.setattr(fundamentals, "_fetch_nse_results", lambda sym: (calls.append("nse") or [qrow(sym, 100, 101)], {}))
    monkeypatch.setattr(fundamentals, "_fetch_yahoo_results", lambda sym, sub: calls.append("yahoo") or [])
    first = fundamentals.quarterly_fundamentals("SBIN")
    assert first["cache"] == "refreshed" and calls == ["nse", "yahoo"]
    second = fundamentals.quarterly_fundamentals("SBIN")          # still >= 70 days old, but already refreshed today
    assert second["cache"] == "hit" and calls == ["nse", "yahoo"]
    get_settings().force_refresh = True
    try:
        assert fundamentals.quarterly_fundamentals("SBIN")["cache"] == "refreshed" and len(calls) == 4
    finally:
        get_settings().force_refresh = False


def test_results_fetch_failure_is_reported_not_zero(monkeypatch):
    monkeypatch.setattr(fundamentals, "_fetch_nse_results", lambda s: (_ for _ in ()).throw(RuntimeError("NSE blocked")))
    monkeypatch.setattr(fundamentals, "_fetch_yahoo_results", lambda s, sub: [])
    out = fundamentals.quarterly_fundamentals("SBIN")
    assert out["status"] == "unavailable" and "NSE blocked" in out["message"] and "import_results" in out["message"]


def test_results_fetch_diagnostics_persist_on_cache_hit(monkeypatch):
    diag = {"raw_rows": 8, "parsed_rows": 1, "sample_raw_keys": ["to_date", "income", "proLossAftTax"]}
    monkeypatch.setattr(fundamentals, "_fetch_nse_results", lambda sym: ([qrow(sym, 20, 100, reported_on="2026-10-14")], diag))
    monkeypatch.setattr(fundamentals, "_fetch_yahoo_results", lambda sym, sub: [])
    first = fundamentals.quarterly_fundamentals("SBIN")
    assert first["cache"] == "refreshed" and first["data"]["last_fetch"]["nse"]["raw_rows"] == 8
    second = fundamentals.quarterly_fundamentals("SBIN")   # cache hit -> diagnostics must STILL be visible in the report
    assert second["cache"] == "hit" and second["data"]["last_fetch"]["nse"]["parsed_rows"] == 1
    assert any(s["source"] == "nse" for s in second["data"]["source_counts"])


def test_results_point_in_time_excludes_unannounced_quarters():
    fq.upsert_quarters([qrow("SBIN", 200, 100), qrow("SBIN", 10, 130, reported_on=(TODAY - timedelta(days=2)).isoformat())])
    out = fundamentals.quarterly_fundamentals("SBIN", as_of=(TODAY - timedelta(days=30)).isoformat())
    assert out["status"] == "ok" and len(out["data"]["quarters"]) == 1


def test_import_csv_history(tmp_path):
    f = tmp_path / "h.csv"
    f.write_text("symbol,period_end,revenue,op_profit,pbt,pat,eps\nSBIN,2026-06-30,100,40,30,20,1\nSBIN,2026-09-30,110,44,33,22,1.1\n")
    assert fq.import_csv(f) == 2
    rows, src, basis = fq.pick_series("SBIN", TODAY)
    assert src == "import" and len(rows) == 2


# ---------------------------------------------------------------- news + events dedupe
def test_news_store_inserts_only_new_items():
    items = [{"title": "Bank profit rises", "publisher": "X", "published_at": "2026-10-20T10:00:00+05:30", "url": "u1"},
             {"title": "Bank   PROFIT rises!", "publisher": "Y", "published_at": "2026-10-20T11:00:00+05:30", "url": "u2"},
             {"title": "RBI keeps rates", "publisher": "X", "published_at": "2026-10-19T10:00:00+05:30", "url": "u3"}]
    assert news_store.upsert("sbi", items) == 2          # the re-worded duplicate headline is skipped
    assert news_store.upsert("sbi", items) == 0
    assert news_store.upsert("hdfc", items) == 2         # same headline under another scope is kept separately


def test_news_search_fetches_once_a_day_and_dedupes(monkeypatch):
    calls = []
    now = now_ist()
    feed = [{"title": "A", "publisher": "P", "published_at": (now - timedelta(hours=3)).isoformat(timespec="seconds"), "url": "1"}]
    monkeypatch.setattr(news, "_fetch", lambda q, d: calls.append(q) or feed)
    a = news.news_search("SBI results")
    b = news.news_search("SBI results")
    assert a["cache"] == "refreshed" and a["new_items"] == 1 and b["cache"] == "hit" and len(calls) == 1 and len(b["data"]) == 1
    get_settings().force_refresh = True
    try:
        c = news.news_search("SBI results")
        assert c["cache"] == "refreshed" and c["new_items"] == 0 and len(c["data"]) == 1      # re-fetched, nothing duplicated
    finally:
        get_settings().force_refresh = False


def test_news_fetch_failure_falls_back_to_stored(monkeypatch):
    news_store.upsert("sbi results", [{"title": "Old", "publisher": "P", "published_at": (now_ist() - timedelta(hours=5)).isoformat(timespec="seconds"), "url": "1"}])
    monkeypatch.setattr(news, "_fetch", lambda q, d: (_ for _ in ()).throw(RuntimeError("offline")))
    out = news.news_search("SBI results")
    assert out["status"] == "ok" and out["cache"] == "stale" and "offline" in out["fetch_error"]


def test_events_stored_once_and_not_duplicated(monkeypatch):
    def fake_get(path, params=None):
        if "announcements" in path:
            return [{"an_dt": (now_ist() - timedelta(days=2)).strftime("%d-%b-%Y %H:%M:%S"), "desc": "Dividend", "attchmntText": "Interim dividend",
                     "attchmntFile": "https://nse/a.pdf", "seq_id": "1"}]
        raise nse.SourceError("blocked")
    monkeypatch.setattr(nse, "nse_get", fake_get)
    first = nse.nse_company_events("SBIN", days=30)
    second = nse.nse_company_events("SBIN", days=30)
    assert first["cache"] == "refreshed" and first["new_events"] == 1 and second["cache"] == "hit" and len(second["data"]) == 1
    get_settings().force_refresh = True
    try:
        assert nse.nse_company_events("SBIN", days=30)["new_events"] == 0
    finally:
        get_settings().force_refresh = False
    assert len(event_store.load("SBIN", now_ist(), 30)) == 1


# ---------------------------------------------------------------- FII / DII
def test_flows_upsert_is_idempotent_per_day():
    row = {"category": "FII/FPI *", "date": "20-Oct-2026", "buy_value_cr": "1,000.5", "sell_value_cr": "1,800.5", "net_value_cr": "-800"}
    assert flows.upsert_rows([row, {**row, "category": "DII **", "net_value_cr": "1200"}]) == 2
    flows.upsert_rows([row])
    assert flows.load_series(datetime(2026, 10, 21).date(), "FII") == [("2026-10-20", -800.0)]


def test_flow_analytics_windows_streaks_and_missing_history():
    short = flows.analyse([("2026-10-16", 100.0), ("2026-10-19", -50.0), ("2026-10-20", -80.0)])
    assert short["net_1d"] == -80.0 and short["streak_days"] == -2 and short["net_5d"] == "insufficient_history"
    series = [(f"2026-09-{d:02d}", 100.0 if d < 21 else -300.0) for d in range(1, 26)]
    full = flows.analyse(series)
    assert full["net_5d"] == -1500.0 and full["net_20d"] == sum(v for _, v in series[-20:]) and full["momentum"] == "worsening"
    assert full["streak_days"] == -5 and full["direction_5d"] == "net selling"


def test_fii_dii_tool_stores_history_and_labels_scope(monkeypatch):
    rows = [{"category": "FII/FPI *", "date": "20-Oct-2026", "buyValue": "1", "sellValue": "2", "netValue": "-800"},
            {"category": "DII **", "date": "20-Oct-2026", "buyValue": "3", "sellValue": "1", "netValue": "1200"}]
    n = {"calls": 0}
    monkeypatch.setattr(nse, "nse_get", lambda *a, **k: n.__setitem__("calls", n["calls"] + 1) or rows)
    first, second = nse.nse_fii_dii(), nse.nse_fii_dii()
    assert first["scope"] == "market_wide_not_stock_specific" and first["cache"] == "refreshed" and second["cache"] == "hit" and n["calls"] == 1
    assert first["data"]["analytics"]["FII"]["net_1d"] == -800.0 and first["data"]["analytics"]["DII"]["net_5d"] == "insufficient_history"


def test_fii_dii_fetch_failure_without_history_is_error(monkeypatch):
    monkeypatch.setattr(nse, "nse_get", lambda *a, **k: (_ for _ in ()).throw(nse.SourceError("blocked")))
    assert nse.nse_fii_dii()["status"] == "error"


# ---------------------------------------------------------------- option chain: support / resistance + snapshots
def test_option_chain_levels_from_oi():
    lv = market.summarize_chain(CHAIN, datetime.now(IST))["levels"]
    assert lv["resistance"][0]["strike"] == 840 and lv["support"][0]["strike"] == 780        # spot 800: calls above, puts below
    assert lv["max_pain_approx"] in (780, 820, 840)


def test_oi_snapshots_feed_weekly_and_monthly_windows():
    t = now_ist()
    market._save_snapshot("SBIN", {"captured_at": (t - timedelta(days=8)).isoformat(timespec="seconds"), "call_oi": 1000, "put_oi": 900,
                                   "pcr": 0.9, "ltp": 800, "support": [], "resistance": [], "max_pain": 800})
    w = market._window_changes("SBIN", 1500, 1200, t)
    assert w["1w"]["call_oi_change"] == 500 and w["1w"]["put_oi_change"] == 300 and w["1m"] == "insufficient_history"
    assert market._window_changes("HDFCBANK", 1, 1, t) == {}


# ---------------------------------------------------------------- multi-timeframe EMA / RSI
def test_timeframe_summary_and_alignment():
    up, down, short = trend_frame(0.004), trend_frame(-0.004), trend_frame(0.004, n=20)
    su, sd = indicators.timeframe_summary(up), indicators.timeframe_summary(down)
    assert su["trend"] == "up" and sd["trend"] == "down" and su["ema200"] is not None and indicators.timeframe_summary(short) is None
    aligned = indicators.mtf_alignment({"15m": su, "1h": su, "1d": su})
    assert aligned["alignment"] == "bullish_aligned"
    assert indicators.mtf_alignment({"15m": sd, "1h": su, "1d": su})["alignment"] == "conflicting"
    thin = indicators.mtf_alignment({"15m": None, "1h": None, "1d": su})
    assert thin["alignment"] == "insufficient_timeframes" and thin["missing"] == ["15m", "1h"]


def test_indicator_tool_returns_three_timeframes(monkeypatch):
    frames = {"D": trend_frame(0.004), "60": trend_frame(0.004, n=250), "15": trend_frame(0.004, n=250)}
    monkeypatch.setattr(indicators, "fetch_candles", lambda sym, res, days, as_of=None: (frames[res], "fake"))
    out = indicators.get_technical_indicators("SBIN")
    mtf = out["data"]["mtf"]
    assert out["status"] == "ok" and mtf["alignment"] == "bullish_aligned" and all(mtf[k]["rsi14"] is not None for k in ("15m", "1h", "1d"))
    assert out["data"]["rule_score_parts"]["mtf_alignment"] == 1.0


def test_indicator_tool_survives_missing_intraday(monkeypatch):
    def fetch(sym, res, days, as_of=None):
        if res != "D":
            raise RuntimeError("no intraday")
        return trend_frame(0.004), "fake"
    monkeypatch.setattr(indicators, "fetch_candles", fetch)
    out = indicators.get_technical_indicators("SBIN")
    assert out["status"] == "ok" and out["data"]["mtf"]["alignment"] == "insufficient_timeframes" and out["data"]["mtf"]["15m"] is None


# ---------------------------------------------------------------- graph level: shared industry score, run storage, refresh
def _patch_tools(monkeypatch, counter: dict):
    tools = fake_tools()
    orig = tools["rbi_policy"]
    tools["rbi_policy"] = lambda **k: counter.__setitem__("rbi", counter.get("rbi", 0) + 1) or orig(**k)
    monkeypatch.setattr(registry, "TOOLS", {**registry.TOOLS, **tools})
    import app.mcp_server.client as c
    monkeypatch.setattr(c, "TOOLS", registry.TOOLS)


ROWS = [{"symbol": s, "company_name": s + " Ltd", "sub_sector": sub} for s, sub in
        [("SBIN", "bank_psu"), ("RBLBANK", "bank_private"), ("HDFCLIFE", "insurance_life")]]


def test_industry_score_computed_once_and_shared(monkeypatch):
    get_settings().llm_force_provider = "stub"
    counter: dict = {}
    _patch_tools(monkeypatch, counter)
    _, ranked = asyncio.run(runner.run_universe(ROWS, as_of="2026-10-20", mode="inprocess"))
    scores = {r.symbol: r.scores["industry"] for r in ranked}
    assert counter["rbi"] == 1 and len(set(scores.values())) == 1 and None not in scores.values()
    assert all("shared sector score" in r.agent_results["industry"].note for r in ranked)
    _, again = asyncio.run(runner.run_universe(ROWS[:1], as_of="2026-10-20", mode="inprocess"))      # next run, same day: cached
    assert counter["rbi"] == 1 and again[0].scores["industry"] == scores["SBIN"]


def test_run_is_saved_with_every_agent_io(monkeypatch):
    get_settings().llm_force_provider = "stub"
    _patch_tools(monkeypatch, {})
    run_id, ranked = asyncio.run(runner.run_universe(ROWS, as_of="2026-10-20", mode="inprocess", scope="specific:SBIN,RBLBANK,HDFCLIFE"))
    assert run_store.list_runs()[0]["run_id"] == run_id and run_store.list_runs()[0]["scope"].startswith("specific")
    rows = run_store.load_run(run_id, "SBIN")
    agents = {r["agent"]: r for r in rows}
    assert set(agents) == {"fundamental", "industry", "indicator", "investment_trend", "option_chain", "decision_maker", "final"}
    f = agents["fundamental"]
    assert f["status"] == "ok" and f["score"] is not None and f["reason"] and f["sources"] and "quarterly_results" in f["context_used"]
    assert any(s["tool"] == "quarterly_fundamentals" for s in f["sources"]) and agents["final"]["extra"]["trade_status"]
    assert len(run_store.load_run(run_id)) == len(rows) + len([r for r in run_store.load_run(run_id) if r["symbol"] != "SBIN"])


def test_refresh_on_load_reports_per_dataset(monkeypatch):
    import app.mcp_server.client as c
    tools = fake_tools()
    tools["nse_fii_dii"] = lambda as_of=None: {"status": "ok", "cache": "refreshed", "data": {}, "source": "fake"}
    monkeypatch.setattr(registry, "TOOLS", {**registry.TOOLS, **tools})
    monkeypatch.setattr(c, "TOOLS", registry.TOOLS)

    async def go(as_of):
        async with c.ToolClient("inprocess") as client:
            return await refresh_job.refresh_stale(client, ROWS, as_of)
    rep = asyncio.run(go(None))
    assert rep["fii_dii (market)"] == {"ok/refreshed": 1} and rep["quarterly results"] == {"ok/hit": 3}
    assert rep["option chain + OI snapshot"]["unavailable/n/a"] == 2        # HDFCLIFE/RBLBANK: no option chain
    assert "skipped" in asyncio.run(go("2026-01-05"))                          # historical as_of: never refreshed
    assert "Refresh" in refresh_job.format_report(rep)
