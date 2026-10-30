"""Offline stub 'LLM' (provider "fake"). Used for the demo run and tests, never for real analysis.

It reads the JSON evidence the agent sends and returns schema-valid JSON with a deterministic,
pseudo-random score. It costs nothing and proves the wiring end-to-end.
"""
from __future__ import annotations

import json
import zlib
from types import SimpleNamespace
from typing import Any

WEIGHTS = {"fundamental": 0.30, "industry": 0.20, "indicators": 0.25, "investment_trend": 0.15, "option_chain": 0.10}


def _score(symbol: str, agent: str) -> float:
    h = zlib.crc32(f"{symbol}:{agent}".encode()) % 1600
    return round(h / 100 - 8.0, 1)  # -8.0 .. +7.9


class FakeChatLLM:
    def __init__(self, agent: str = "") -> None:
        self.agent = agent

    async def ainvoke(self, messages: list[Any], **_: Any) -> Any:
        try:
            payload = json.loads(messages[-1].content)
        except Exception:
            payload = {}
        symbol = payload.get("symbol", "?")
        if self.agent == "decision_maker":
            scores = {k: v["score"] for k, v in payload.get("specialists", {}).items() if v.get("score") is not None}
            wsum = sum(WEIGHTS[k] for k in scores) or 1
            final = sum(WEIGHTS[k] * v for k, v in scores.items()) / wsum
            pos = [v > 0 for v in scores.values()]
            body = {
                "direction": "BULLISH" if all(pos) else "BEARISH" if not any(pos) else "MIXED",
                "final_score": round(final, 2),
                "trade_status": "POTENTIAL_ENTRY" if final >= 3 else "WAIT_FOR_SETUP" if final > 0 else "AVOID",
                "confidence": 0.6,
                "overextended": False,
                "reasoning_summary": f"[STUB] {symbol}: weighted view of {len(scores)} specialist scores.",
                "agreements": [], "conflicts": [], "risk_flags": ["stub-llm"],
                "next_catalyst": None, "invalidation": "stub",
            }
        else:
            body = {
                "score": _score(symbol, self.agent),
                "confidence": 0.5,
                "reasoning": f"[STUB] deterministic placeholder for {self.agent} on {symbol}.",
                "key_points": ["stub"], "risk_flags": [], "catalysts": [],
            }
            if self.agent == "indicators":
                body.update(trend_state="sideways", setup_quality="medium", overextended=False)
            if self.agent == "investment_trend":
                body.update(flow_direction="neutral", evidence_level="market_wide")
            if self.agent == "option_chain":
                body.update(expiry=None, window="1d", caveats=["stub"])
        return SimpleNamespace(content=json.dumps(body))
