import pytest

from app.schemas import AGENT_NAMES, AgentResult, DecisionOutput
from app.scoring import policy

CFG = policy.load_config()


def res(agent, score, status="ok", **extra):
    return AgentResult(agent=agent, symbol="X", status=status, score=score if status == "ok" else None, extra=extra)


def full(**scores):
    base = dict(fundamental=5, industry=3, indicator=4, trader=3, rs_agent=2,
                quant_risk=1, promoter_intel=2, catalyst_quality=2, investment_trend=2, option_chain=1)
    base.update(scores)
    return {k: res(k, v) if v is not None else res(k, None, "unavailable") for k, v in base.items()}


def test_weights_sum_to_one_and_cover_all_agents():
    assert set(CFG["weights"]) == set(AGENT_NAMES)
    assert sum(CFG["weights"].values()) == pytest.approx(1.0)


def test_weighted_score_matches_formula():
    p = policy.compute_policy(full(), CFG)
    w = CFG["weights"]
    expected = (w["fundamental"]*5 + w["industry"]*3 + w["indicator"]*4 + w["trader"]*3 +
                w["rs_agent"]*2 + w["quant_risk"]*1 + w["promoter_intel"]*2 +
                w["catalyst_quality"]*2 + w["investment_trend"]*2 + w["option_chain"]*1)
    assert p["policy_score"] == pytest.approx(expected, abs=0.01)
    assert p["consensus_status"] == "BULLISH_CONSENSUS" and p["data_status"] == "COMPLETE"


def test_missing_optional_is_partial_and_not_treated_as_zero():
    w = CFG["weights"]
    p = policy.compute_policy(full(option_chain=None), CFG)
    assert p["data_status"] == "PARTIAL"
    assert p["data_completeness"] == pytest.approx(1.0 - w["option_chain"], abs=0.001)


def test_missing_required_is_insufficient():
    p = policy.compute_policy(full(fundamental=None), CFG)
    assert p["consensus_status"] == "INSUFFICIENT_DATA" and "fundamental" in p["missing_required"]


def test_low_completeness_is_insufficient():
    # drop enough non-required agents to go below 0.70
    p = policy.compute_policy(full(quant_risk=None, option_chain=None, catalyst_quality=None,
                                   rs_agent=None, promoter_intel=None), CFG)
    assert p["data_status"] == "INSUFFICIENT"


@pytest.mark.parametrize("scores,expected", [
    (dict(fundamental=5, industry=-2), "MIXED"),
    (dict(fundamental=-5, industry=-2, indicator=-1, trader=-1, rs_agent=-1,
          quant_risk=-1, promoter_intel=-1, catalyst_quality=-1, investment_trend=-3, option_chain=-4), "BEARISH_CONSENSUS"),
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
    p = policy.compute_policy(full(fundamental=-5, industry=-2, indicator=-1, trader=-1,
                                   rs_agent=-1, quant_risk=-1, promoter_intel=-1,
                                   catalyst_quality=-1, investment_trend=-3, option_chain=-4), CFG)
    assert policy.validate_trade_status("POTENTIAL_ENTRY", p, CFG)[0] == "AVOID"


def test_insufficient_data_overrides_llm():
    p = policy.compute_policy(full(fundamental=None), CFG)
    assert policy.validate_trade_status("POTENTIAL_ENTRY", p, CFG)[0] == "INSUFFICIENT_DATA"


def test_finalize_records_discrepancy():
    dec = DecisionOutput(direction_view="BULLISH", proposed_score=9.5, trade_status="POTENTIAL_ENTRY", confidence=0.8, reasoning="r")
    out = policy.finalize(run_id="r", symbol="X", meta={}, as_of="2026-10-20", results=full(), decision=dec, cfg=CFG)
    assert out.score_discrepancy and out.score_discrepancy > 2 and any("differs" in x for x in out.risk_flags)
    assert out.final_score == pytest.approx(3.07, abs=0.01)  # authoritative score is the computed one


def test_rank_orders_valid_first_and_never_ranks_invalid_as_zero():
    a = policy.finalize(run_id="r", symbol="A", meta={}, as_of="t", results=full(), decision=None, cfg=CFG)
    b = policy.finalize(run_id="r", symbol="B", meta={}, as_of="t",
                        results=full(fundamental=-8, industry=-8, indicator=-8, trader=-8,
                                     rs_agent=-8, quant_risk=-8, promoter_intel=-8,
                                     catalyst_quality=-8, investment_trend=-8, option_chain=-8),
                        decision=None, cfg=CFG)
    bad = policy.finalize(run_id="r", symbol="C", meta={}, as_of="t", results={}, decision=None, cfg=CFG, error="boom")
    ranked = policy.rank([bad, b, a])
    assert [s.symbol for s in ranked] == ["A", "B", "C"] and not ranked[-1].valid
