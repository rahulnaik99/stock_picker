import pytest

from app.schemas import AGENT_NAMES, AgentResult, DecisionOutput
from app.scoring import policy

CFG = policy.load_config()


def res(agent, score, status="ok", **extra):
    return AgentResult(agent=agent, symbol="X", status=status, score=score if status == "ok" else None, extra=extra)


def full(**scores):
    base = dict(fundamental=5, industry=3, indicator=4, investment_trend=2, option_chain=1)
    base.update(scores)
    return {k: res(k, v) if v is not None else res(k, None, "unavailable") for k, v in base.items()}


def test_weights_sum_to_one_and_cover_all_agents():
    assert set(CFG["weights"]) == set(AGENT_NAMES)
    assert sum(CFG["weights"].values()) == pytest.approx(1.0)


def test_weighted_score_matches_formula():
    p = policy.compute_policy(full(), CFG)
    expected = 0.30 * 5 + 0.20 * 3 + 0.25 * 4 + 0.15 * 2 + 0.10 * 1
    assert p["policy_score"] == pytest.approx(expected, abs=0.01)
    assert p["consensus_status"] == "BULLISH_CONSENSUS" and p["data_status"] == "COMPLETE"


def test_missing_optional_is_partial_and_not_treated_as_zero():
    p = policy.compute_policy(full(option_chain=None), CFG)
    assert p["data_status"] == "PARTIAL" and p["data_completeness"] == pytest.approx(0.9)
    assert p["policy_score"] == pytest.approx((0.30 * 5 + 0.20 * 3 + 0.25 * 4 + 0.15 * 2) / 0.9, abs=0.01)


def test_missing_required_is_insufficient():
    p = policy.compute_policy(full(fundamental=None), CFG)
    assert p["consensus_status"] == "INSUFFICIENT_DATA" and "fundamental" in p["missing_required"]


def test_low_completeness_is_insufficient():
    p = policy.compute_policy(full(investment_trend=None, option_chain=None), CFG)  # 0.75 < 0.80
    assert p["data_status"] == "INSUFFICIENT"


@pytest.mark.parametrize("scores,expected", [
    (dict(fundamental=5, industry=-2), "MIXED"),
    (dict(fundamental=-5, industry=-2, indicator=-1, investment_trend=-3, option_chain=-4), "BEARISH_CONSENSUS"),
    (dict(industry=0), "NEUTRAL_OR_MIXED"),
])
def test_direction_gate(scores, expected):
    assert policy.compute_policy(full(**scores), CFG)["consensus_status"] == expected


def test_error_status_is_not_usable():
    r = full()
    r["indicator"] = AgentResult(agent="indicator", symbol="X", status="error", reasoning="boom")
    assert policy.compute_policy(r, CFG)["consensus_status"] == "INSUFFICIENT_DATA"


def test_potential_entry_downgraded_when_extended():
    r = full(); r["indicator"] = res("indicator", 4, extended=True)
    p = policy.compute_policy(r, CFG)
    status, notes = policy.validate_trade_status("POTENTIAL_ENTRY", p, CFG)
    assert status == "WAIT_FOR_SETUP" and "extended" in notes[0]


def test_potential_entry_kept_when_clean():
    p = policy.compute_policy(full(), CFG)
    assert policy.validate_trade_status("POTENTIAL_ENTRY", p, CFG)[0] == "POTENTIAL_ENTRY"


def test_bearish_consensus_forces_avoid():
    p = policy.compute_policy(full(fundamental=-5, industry=-2, indicator=-1, investment_trend=-3, option_chain=-4), CFG)
    assert policy.validate_trade_status("POTENTIAL_ENTRY", p, CFG)[0] == "AVOID"


def test_insufficient_data_overrides_llm():
    p = policy.compute_policy(full(fundamental=None), CFG)
    assert policy.validate_trade_status("POTENTIAL_ENTRY", p, CFG)[0] == "INSUFFICIENT_DATA"


def test_finalize_records_discrepancy():
    dec = DecisionOutput(direction_view="BULLISH", proposed_score=9.5, trade_status="POTENTIAL_ENTRY", confidence=0.8, reasoning="r")
    out = policy.finalize(run_id="r", symbol="X", meta={}, as_of="2026-10-20", results=full(), decision=dec, cfg=CFG)
    assert out.score_discrepancy and out.score_discrepancy > 2 and any("differs" in x for x in out.risk_flags)
    assert out.final_score == pytest.approx(3.5, abs=0.01)  # authoritative score is the computed one


def test_rank_orders_valid_first_and_never_ranks_invalid_as_zero():
    a = policy.finalize(run_id="r", symbol="A", meta={}, as_of="t", results=full(), decision=None, cfg=CFG)
    b = policy.finalize(run_id="r", symbol="B", meta={}, as_of="t", results=full(fundamental=-8, industry=-8, indicator=-8, investment_trend=-8, option_chain=-8), decision=None, cfg=CFG)
    bad = policy.finalize(run_id="r", symbol="C", meta={}, as_of="t", results={}, decision=None, cfg=CFG, error="boom")
    ranked = policy.rank([bad, b, a])
    assert [s.symbol for s in ranked] == ["A", "B", "C"] and not ranked[-1].valid
