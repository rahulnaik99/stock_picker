"""Single source of truth for tools. server.py exposes these over MCP; the in-process client calls them directly."""
from __future__ import annotations

from typing import Any, Callable

from app.mcp_server.tools import fundamentals, indicators, market, news, nse, rates
from app.mcp_server.tools import relative_strength, quant_risk
from app.mcp_server.tools import indianapi

TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    # "get_candles" intentionally omitted — no agent declares it in TOOLS; kept in market.py for CLI/scripts only.
    "get_technical_indicators": indicators.get_technical_indicators,
    "get_demand_supply_zones":  indicators.get_demand_supply_zones,
    "get_option_chain":         market.get_option_chain,
    "get_relative_strength":    relative_strength.get_relative_strength,
    "get_quant_risk":           quant_risk.get_quant_risk,
    "nse_company_events":       nse.nse_company_events,
    "nse_fii_dii":              nse.nse_fii_dii,
    "nse_deals":                nse.nse_deals,
    "nse_shareholding":         nse.nse_shareholding,
    "nse_promoter_pledge":      nse.nse_promoter_pledge,
    "yf_fundamentals":          fundamentals.yf_fundamentals,
    "quarterly_fundamentals":   fundamentals.quarterly_fundamentals,
    "rbi_policy":               rates.rbi_policy,
    "foreign_rates":            rates.foreign_rates,
    "macro_snapshot":           rates.macro_snapshot,
    "policy_calendar":          rates.policy_calendar,
    "news_search":              news.news_search,
    "indianapi_news":           indianapi.indianapi_news,
}
