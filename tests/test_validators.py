"""Deterministic validation layer: freshness guard + per-agent consistency flags. All offline."""
from __future__ import annotations

from app.schemas import AgentResult
from app.scoring import validators


def _r(agent, status="ok", score=1.0, confidence=0.6, evidence=None, extra=None):
    return AgentResult(agent=agent, symbol="CANBK", status=status, score=score, confidence=confidence,
                       evidence=evidence or [], extra=extra or {})


# ---------------------------------------------------------------- results freshness
def test_results_freshness_flags_a_newer_filing_than_the_series():
    events = [{"event_type": "results", "event_date": "2026-10-14"},   # Sep-30 quarter filed
              {"event_type": "other", "event_date": "2026-10-23"}]
    flag = validators.results_freshness(events, latest_period="2026-06-30")
    assert flag and "2026-10-14" in flag and "2026-06-30" in flag


def test_results_freshness_quiet_when_series_is_current():
    events = [{"event_type": "results", "event_date": "2026-10-14"}]
    assert validators.results_freshness(events, latest_period="2026-09-30") is None   # within 55 days
    assert validators.results_freshness([], latest_period="2026-06-30") is None       # no results event
    assert validators.results_freshness(events, latest_period=None) is None


# ---------------------------------------------------------------- per-agent consistency
def test_agent_flags_catches_inconsistencies():
    assert validators.agent_flags(_r("fundamental", status="ok", score=None)) == ["fundamental: status 'ok' but score is null"]
    assert any("not null" in f for f in validators.agent_flags(_r("industry", status="unavailable", score=2.0)))
    assert any("no evidence" in f for f in validators.agent_flags(_r("indicator", status="ok", score=-5.0, evidence=[])))
    assert validators.agent_flags(_r("option_chain", status="ok", score=2.0, evidence=[{"claim": "x"}])) == []


def test_run_validations_aggregates_and_surfaces_freshness():
    results = {
        "fundamental": _r("fundamental", status="ok", score=3.0, evidence=[{"claim": "x"}],
                          extra={"data_freshness_flag": "STALE FUNDAMENTALS: a results filing dated 2026-10-14 ..."}),
        "indicator": _r("indicator", status="unavailable", score=-4.0),  # inconsistent
    }
    flags = validators.run_validations(results)
    assert any("STALE FUNDAMENTALS" in f for f in flags)
    assert any(f.startswith("indicator:") for f in flags)
