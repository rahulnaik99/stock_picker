import asyncio
import json
from datetime import datetime

import openpyxl
import pytest

from app.Agents import base
from app.Agents.Technical.TrendFlow import agent as trend
from app.export.writer import export
from app.graph import runner
from app.ledger.events import EventLedger
from app.mcp_server import registry
from app.mcp_server.client import ToolClient
from app.mcp_server.tools.common import IST, ok, unavailable
from app.schemas import AgentResult
from app.service.models import llm_factory
from app.service.settings import get_settings
from tests.fakes import fake_tools


# ---- JSON extraction / LLM validation -------------------------------------------------------------
@pytest.mark.parametrize("text", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! Here you go: {"a": 1} hope it helps', '{"a": {"b": "}"}}'])
def test_extract_json_variants(text):
    assert "a" in base.extract_json(text)


def test_extract_json_rejects_garbage():
    with pytest.raises(ValueError):
        base.extract_json("no json here")


class ScriptedLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    async def ainvoke(self, messages, **_):
        self.calls += 1
        return type("R", (), {"content": self.replies.pop(0)})()


def patch_llm(monkeypatch, llm):
    spec = llm_factory.ModelSpec("a", "stub", "m", 0, 10)
    monkeypatch.setattr(base, "get_llm", lambda key: (llm, spec))


def run_analyze():
    return asyncio.run(base.analyze(agent="a", llm_key="a", symbol="X", system_prompt="s", evidence={}, tool_results={"t": ok("s", 1)}, context={}))


def test_analyze_retries_once_on_bad_json(monkeypatch):
    llm = ScriptedLLM(["not json", '{"score": 4, "reasoning": "r"}'])
    patch_llm(monkeypatch, llm)
    r = run_analyze()
    assert r.status == "ok" and r.score == 4 and llm.calls == 2


def test_analyze_gives_error_status_not_zero_after_two_failures(monkeypatch):
    patch_llm(monkeypatch, ScriptedLLM(["x", "y"]))
    r = run_analyze()
    assert r.status == "error" and r.score is None


def test_analyze_clamps_out_of_range_and_normalises_confidence(monkeypatch):
    patch_llm(monkeypatch, ScriptedLLM(['{"score": 14, "reasoning": "r", "confidence": 80}']))
    r = run_analyze()
    assert r.score == 10 and r.confidence == 0.8 and "clamped" in r.note


def test_analyze_survives_provider_exception(monkeypatch):
    class Boom:
        async def ainvoke(self, *a, **k):
            raise ConnectionError("down")
    patch_llm(monkeypatch, Boom())
    assert run_analyze().status == "error"


# ---- investment trend rules ----------------------------------------------------------------------
def test_market_wide_only_score_is_capped(monkeypatch):
    patch_llm(monkeypatch, ScriptedLLM(['{"score": 9, "reasoning": "FIIs buying"}']))
    tools = ToolClient("inprocess").for_agent(trend.TOOLS)
    t = fake_tools()
    t["nse_deals"] = lambda symbol, days=30, as_of=None: unavailable("fake", "x")
    monkeypatch.setattr(registry, "TOOLS", {**registry.TOOLS, **t})
    import app.mcp_server.client as c
    monkeypatch.setattr(c, "TOOLS", registry.TOOLS)
    r = asyncio.run(trend.run("SBIN", {}, tools, "2026-10-20"))
    assert r.score == 3.0 and r.extra["evidence_level"] == "market_wide_only" and "capped" in r.note


def test_no_flow_evidence_is_unavailable_without_calling_llm(monkeypatch):
    llm = ScriptedLLM([])
    patch_llm(monkeypatch, llm)
    dead = {k: (lambda *a, **k: unavailable("fake", "down")) for k in ("nse_fii_dii", "nse_deals", "nse_shareholding")}
    import app.mcp_server.client as c
    monkeypatch.setattr(c, "TOOLS", {**registry.TOOLS, **dead})
    r = asyncio.run(trend.run("SBIN", {}, ToolClient("inprocess").for_agent(trend.TOOLS), None))
    assert r.status == "unavailable" and r.score is None and llm.calls == 0


# ---- tool client ---------------------------------------------------------------------------------
def test_agent_cannot_call_tool_outside_allow_list():
    tools = ToolClient("inprocess").for_agent(["policy_calendar"])
    with pytest.raises(PermissionError):
        asyncio.run(tools.call("nse_fii_dii"))


def test_unknown_tool_in_allow_list_rejected():
    with pytest.raises(ValueError):
        ToolClient("inprocess").for_agent(["nope"])


def test_real_mcp_server_over_stdio_exposes_all_tools():
    async def go():
        async with ToolClient("stdio") as c:
            r = await c.call("policy_calendar", days_ahead=200, as_of="2026-10-20")
            return r
    r = asyncio.run(go())
    assert r["status"] == "ok" and r["data"]


def test_every_agent_has_explicit_model_entry():
    cfg = llm_factory.load_model_config()
    for key in ("fundamental", "industry", "indicator", "trend_flow", "option_chain", "decision_maker"):
        assert key in cfg, f"model_config.json has no entry for {key} (it would silently use 'default')"


# ---- ledger --------------------------------------------------------------------------------------
def ev(eid="e1", summary="s", avail="2026-10-10T10:00:00+05:30", date="2026-10-10"):
    return {"event_id": eid, "symbol": "SBIN", "exchange": "NSE", "event_type": "dividend", "event_date": date, "event_status": "announced",
            "event_summary": summary, "source_url": "u", "published_at": avail, "retrieved_at": avail, "available_at": avail}


def test_ledger_upsert_dedupes_and_keeps_first_seen():
    L = EventLedger()
    assert L.upsert([ev()]) == (1, 0)
    assert L.upsert([ev(summary="changed", avail="2026-10-18T10:00:00+05:30")]) == (0, 1)
    rows = L.all_for(["SBIN"])
    assert len(rows) == 1 and rows.iloc[0]["event_summary"] == "changed" and rows.iloc[0]["available_at"].startswith("2026-10-10")


def test_ledger_point_in_time_query_and_impacts():
    L = EventLedger()
    L.upsert([ev("old", avail="2026-10-10T10:00:00+05:30"), ev("later", avail="2026-10-25T10:00:00+05:30", date="2026-10-25")])
    t = datetime(2026, 10, 20, tzinfo=IST)
    assert [e["event_id"] for e in L.query("SBIN", t)] == ["old"]
    assert L.update_impacts([{"event_id": "old", "impact_score": 2, "reasoning": "good"}]) == 1
    assert L.query("SBIN", t)[0]["event_impact_score"] == "2"
    L.upsert([ev("old")])  # re-poll must not wipe the analyst score
    assert L.query("SBIN", t)[0]["event_impact_score"] == "2"


# ---- whole graph, offline ------------------------------------------------------------------------
def test_full_run_ranks_isolates_failures_and_exports(monkeypatch, tmp_path):
    get_settings().llm_force_provider = "stub"
    tools = fake_tools()
    monkeypatch.setattr(registry, "TOOLS", {**registry.TOOLS, **tools})
    import app.mcp_server.client as c
    monkeypatch.setattr(c, "TOOLS", registry.TOOLS)
    rows = [{"symbol": s, "company_name": s + " Ltd", "sub_sector": sub} for s, sub in
            [("SBIN", "bank_psu"), ("RBLBANK", "bank_private"), ("HDFCLIFE", "insurance_life"), ("BROKENCO", "nbfc")]]
    run_id, ranked = asyncio.run(runner.run_universe(rows, as_of="2026-10-20", mode="inprocess"))
    by = {r.symbol: r for r in ranked}
    assert len(ranked) == 4 and not by["BROKENCO"].valid and by["BROKENCO"].trade_status == "INSUFFICIENT_DATA"
    assert by["SBIN"].valid and by["HDFCLIFE"].agent_status["option_chain"] == "unavailable" and by["HDFCLIFE"].data_status == "PARTIAL"
    valid_scores = [r.final_score for r in ranked if r.valid]
    assert valid_scores == sorted(valid_scores, reverse=True) and ranked[-1].symbol == "BROKENCO"
    out = export(ranked, run_id, tmp_path / "out")
    wb = openpyxl.load_workbook(out["xlsx"])
    assert wb.sheetnames == ["Rankings", "Agent Scores", "Events", "Data Quality", "Configuration"]
    head = [c.value for c in wb["Rankings"][1]]
    for col in ("rank", "symbol", "final_score", "direction", "consensus_status", "trade_status", "data_completeness", "run_id"):
        assert col in head
    assert out["csv"].read_text().splitlines()[0].startswith("rank,")
