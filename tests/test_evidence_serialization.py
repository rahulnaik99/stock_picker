import json

from app.Agents.base import _serialize_evidence_payload


def test_oversized_evidence_is_valid_json_and_within_budget():
    payload = {"stock": {"symbol": "TEST"}, "evidence": {"news": {"status": "ok", "source": "rss", "data": ["x" * 3000 for _ in range(100)]}}}
    text = _serialize_evidence_payload(payload, 2000)
    parsed = json.loads(text)
    assert len(text) <= 2000
    assert parsed["stock"]["symbol"] == "TEST"
    assert "_evidence_truncation" in parsed
