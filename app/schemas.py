"""Typed results passed between agents, the graph, scoring and export."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

AgentName = Literal["fundamental", "industry", "indicator", "trader", "rs_agent", "quant_risk",
                    "promoter_intel", "catalyst_quality", "investment_trend", "option_chain"]
AGENT_NAMES: list[str] = ["fundamental", "industry", "indicator", "trader", "rs_agent", "quant_risk",
                          "promoter_intel", "catalyst_quality", "investment_trend", "option_chain"]
Status = Literal["ok", "unavailable", "error"]


class LLMScoreOutput(BaseModel):
    """What a specialist LLM must return (validated; anything else is rejected and retried once)."""
    score: float
    confidence: float = Field(0.5, ge=0, le=1)
    reasoning: str
    evidence: list[Any] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:  # some models answer 0-100
        return v / 100 if isinstance(v, (int, float)) and v > 1 else v


class LLMAdjustOutput(BaseModel):
    """Baseline mode: Python computes a deterministic baseline from the evidence and the LLM returns a bounded
    ADJUSTMENT to it (plus classifications), instead of inventing an absolute score. `score` is tolerated for models
    that still emit an absolute number; code converts it to an adjustment."""
    score_adjustment: float | None = None
    score: float | None = None
    confidence: float = Field(0.5, ge=0, le=1)
    reasoning: str
    evidence: list[Any] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        return v / 100 if isinstance(v, (int, float)) and v > 1 else v


class AgentResult(BaseModel):
    agent: str
    symbol: str
    status: Status
    score: float | None = None            # None when status != ok: never a silent 0
    confidence: float = 0.0
    reasoning: str = ""
    evidence: list[Any] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    sources: list[dict[str, Any]] = Field(default_factory=list)   # [{tool, status, source, retrieved_at}]
    provider: str = ""
    model: str = ""
    duration_ms: float = 0.0
    input_tokens: int = 0                 # provider usage when available, else char/4 estimate (summed over retries)
    output_tokens: int = 0
    note: str = ""
    context_used: str = ""                # the exact JSON the LLM was given (stored per run for audit / UI)


class DecisionOutput(BaseModel):
    direction_view: str = "MIXED"
    proposed_score: float | None = None
    trade_status: str = "WAIT_FOR_SETUP"
    confidence: float = Field(0.5, ge=0, le=1)
    reasoning: str = ""
    agreements: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_catalyst: str | None = None
    invalidation: str | None = None
    # Set by code after the LLM call (not part of the model's JSON contract); used for persistence/telemetry.
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class StockResult(BaseModel):
    run_id: str
    symbol: str
    company_name: str = ""
    sub_sector: str = ""
    analysis_timestamp: str
    scores: dict[str, float | None] = Field(default_factory=dict)
    agent_status: dict[str, str] = Field(default_factory=dict)
    final_score: float | None = None
    llm_proposed_score: float | None = None
    score_discrepancy: float | None = None
    direction: str = "INSUFFICIENT_DATA"
    consensus_status: str = "INSUFFICIENT_DATA"
    data_status: str = "INSUFFICIENT"           # COMPLETE | PARTIAL | INSUFFICIENT
    data_completeness: float = 0.0
    confidence: float = 0.0
    trade_status: str = "INSUFFICIENT_DATA"
    status_adjustments: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    next_catalyst: str | None = None
    source_freshness: str = ""
    valid: bool = False                          # eligible for ranking
    error: str | None = None
    agent_results: dict[str, AgentResult] = Field(default_factory=dict)
    decision: DecisionOutput | None = None
    weights_version: str = ""
