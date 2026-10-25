from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class StockState(TypedDict, total=False):
    run_id: str
    as_of: str                                       # ISO timestamp, the point-in-time cutoff
    stock: Any                                       # config.Stock
    results: Annotated[dict, operator.or_]           # agent name -> AgentResult dict (merged across parallel nodes)
    decision: Any                                    # DecisionVerdict | None
    final: dict                                      # validated record
