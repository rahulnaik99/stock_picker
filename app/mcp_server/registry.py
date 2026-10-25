"""Single source of truth for tools. server.py exposes these over MCP; the in-process client calls them directly."""
from __future__ import annotations

from typing import Any, Callable

from app.mcp_server.tools import fundamentals, indicators, market, news, nse, rates

TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_candles": market.get_candles,
    "get_technical_indicators": indicators.get_technical_indicators,
    "get_option_chain": market.get_option_chain,
    "nse_company_events": nse.nse_company_events,
    "nse_fii_dii": nse.nse_fii_dii,
    "nse_deals": nse.nse_deals,
    "nse_shareholding": nse.nse_shareholding,
    "yf_fundamentals": fundamentals.yf_fundamentals,
    "quarterly_fundamentals": fundamentals.quarterly_fundamentals,
    "rbi_policy": rates.rbi_policy,
    "foreign_rates": rates.foreign_rates,
    "macro_snapshot": rates.macro_snapshot,
    "policy_calendar": rates.policy_calendar,
    "news_search": news.news_search,
}
