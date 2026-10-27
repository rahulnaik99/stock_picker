"""Quick bench: run nimble-v3:latest through the three task types the pipeline actually asks of its models.
Run this BEFORE wiring it in so you know exactly where it helps and where it doesn't.

Usage (from repo root, venv active, Ollama running):
    python scripts/bench_nimble.py
"""
from __future__ import annotations

import json, time
from langchain_openai import ChatOpenAI

OLLAMA_BASE = "http://localhost:11434"
MODEL = "nimble-v3:latest"

def _call(system: str, user: str, json_mode: bool = True) -> tuple[str, float]:
    llm = ChatOpenAI(
        model=MODEL, base_url=f"{OLLAMA_BASE}/v1", api_key="ollama",
        temperature=0, timeout=60,
        model_kwargs={"response_format": {"type": "json_object"}} if json_mode else {},
    )
    t0 = time.perf_counter()
    resp = llm.invoke([("system", system), ("user", user)])
    ms = round((time.perf_counter() - t0) * 1000)
    return resp.content, ms


# ─────────────────────────────────────────────── TASK 1: news sentiment
print("\n── TASK 1: news classification (Fundamental agent)")
HEADLINES = [
    "Canara Bank posts 8.40 percent Q2 profit growth",
    "Transaction of Canbank Factors to be over before March",   # subtle: divestment, not distress
    "Canara Bank expects strong retail lending during festive season",
    "PSU banking stocks rally: Nifty PSU bank jumps over 2%",
    "Bank of India Q2 profit rises 25% on strong NII",
]
SYS_NEWS = (
    "You are a financial news classifier. For each headline decide: positive / negative / neutral. "
    'Return ONLY JSON: {"classifications":[{"headline":"...","label":"positive|negative|neutral","reason":"one sentence"}]}'
)
USER_NEWS = "Headlines:\n" + "\n".join(f"- {h}" for h in HEADLINES)
out, ms = _call(SYS_NEWS, USER_NEWS)
print(f"  {ms} ms")
try:
    data = json.loads(out)
    for c in data.get("classifications", []):
        print(f"  [{c.get('label','-'):8}] {c.get('headline','')[:60]}")
        print(f"           → {c.get('reason','')}")
except json.JSONDecodeError:
    print("  RAW (not JSON):", out[:300])


# ─────────────────────────────────────────────── TASK 2: technical setup quality
print("\n── TASK 2: trade setup classification (Trader agent)")
SYS_TRADE = (
    "You classify a stock's technical trade setup. "
    "Given zone data, return ONLY JSON: "
    '{"score_adjustment": <-2 to +2 float>, "confidence": <0-1>, '
    '"lean": "bullish|bearish|neutral", "reasoning": "1 sentence"}'
)
USER_TRADE = json.dumps({
    "close": 116.16, "ema_stack": "bearish", "position": "mid_range",
    "nearest_demand": {"low": 114, "high": 116, "strength": 72, "dist_pct": -0.3},
    "nearest_supply": {"low": 125, "high": 126, "strength": 60, "dist_pct": 7.6},
    "volume_context": {"trend": "contracting", "ratio": 0.82},
    "mtf_bias": "bearish_aligned",
    "baseline": {"score": -2.5, "max_adjustment": 2.0}
})
out2, ms2 = _call(SYS_TRADE, USER_TRADE)
print(f"  {ms2} ms")
try:
    d = json.loads(out2)
    print(f"  score_adjustment: {d.get('score_adjustment')}  lean: {d.get('lean')}  confidence: {d.get('confidence')}")
    print(f"  reasoning: {d.get('reasoning')}")
except json.JSONDecodeError:
    print("  RAW:", out2[:300])


# ─────────────────────────────────────────────── TASK 3: final decision reconciliation
print("\n── TASK 3: multi-agent reconciliation (Decision Maker)")
SYS_DM = (
    "You reconcile specialist agent scores for an Indian stock and return a final verdict. "
    "Return ONLY JSON: "
    '{"direction_view": "BULLISH|BEARISH|MIXED|INSUFFICIENT_DATA", '
    '"proposed_score": <-10 to +10>, '
    '"trade_status": "POTENTIAL_ENTRY|WAIT_FOR_SETUP|AVOID|INSUFFICIENT_DATA", '
    '"confidence": <0-1>, '
    '"reasoning": "2-3 sentences"}'
)
USER_DM = json.dumps({
    "symbol": "CANBK", "as_of": "2026-10-26",
    "specialists": {
        "fundamental": {"score": 4.0, "confidence": 0.85},
        "industry": {"score": -2.0, "confidence": 0.7},
        "indicator": {"score": -6.5, "confidence": 0.7},
        "trader": {"score": -1.5, "confidence": 0.6},
        "investment_trend": {"score": 1.0, "confidence": 0.5},
        "option_chain": {"score": -1.0, "confidence": 0.5},
    },
    "policy": {"policy_score": -0.38, "consensus_status": "MIXED", "data_completeness": 1.0, "validation_flags": []},
})
out3, ms3 = _call(SYS_DM, USER_DM)
print(f"  {ms3} ms")
try:
    d = json.loads(out3)
    print(f"  direction: {d.get('direction_view')}  score: {d.get('proposed_score')}  status: {d.get('trade_status')}")
    print(f"  reasoning: {d.get('reasoning')}")
except json.JSONDecodeError:
    print("  RAW:", out3[:300])


# ─────────────────────────────────────────────── summary
print(f"\n── Speed summary vs nemotron-e4b-16k (rough real-world: ~8-15s/agent)")
print(f"  news classification : {ms} ms")
print(f"  trade setup         : {ms2} ms")
print(f"  decision reconcile  : {ms3} ms")
print("\nLook for: correct sentiment on the Canbank Factors headline (neutral/mildly-positive, NOT negative)")
print("          correct bearish lean for the trade setup (ema bearish, contracting volume)")
print("          WAIT_FOR_SETUP or MIXED for the decision (strong fundamental vs bearish technicals)")
