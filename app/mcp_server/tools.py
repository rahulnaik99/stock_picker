"""The tool catalogue. ONE implementation, exposed two ways:
  - as MCP tools by app/mcp_server/server.py (what agents normally use),
  - directly via TOOLS (MCP_MODE=inproc, tests).

Every tool returns an envelope: {status: ok|unavailable|error, source, retrieved_at, data|reason}.
Tools never raise and never invent data.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from app.mcp_server import indicators
from app.mcp_server.providers import fred, fyers, news, nse, rbi, yahoo
from app.mcp_server.providers.common import (ProviderError, Unavailable, envelope, ok, parse_as_of, safe)
from app.service.config import load_sectors
from app.service.logger import get_logger

log = get_logger(__name__)
TOOLS: dict[str, Callable[..., dict]] = {}


def tool(fn: Callable[..., dict]) -> Callable[..., dict]:
    TOOLS[fn.__name__] = fn
    return fn


def _iso(as_of: str | None) -> str:
    return parse_as_of(as_of).isoformat()


# ----------------------------------------------------------------------------- technical
@tool
@safe("fyers|yahoo")
def get_technical_pack(symbol: str, as_of: str | None = None) -> dict:
    """Deterministic technical indicators for an NSE symbol (EMA20/50/200, RSI, ADX, ATR, MACD, VWAP,
    volume ratio, pivots, Fibonacci, support/resistance, extension flag, rule_score) computed from
    completed candles. Source is FYERS; Yahoo is used (and labelled) only if FYERS fails."""
    when = parse_as_of(as_of)
    warnings: list[str] = []
    try:
        daily, intraday = fyers.candles_bundle(symbol, when)
        source = "fyers"
    except (Unavailable, ProviderError) as e:
        warnings.append(f"FYERS failed ({e}); fell back to Yahoo for ALL candles (no provider mixing)")
        log.warning("%s: %s", symbol, warnings[-1])
        daily, intraday = yahoo.candles_bundle(symbol, when)
        source = "yahoo"
    pack = indicators.build_pack(daily, intraday, when)
    pack["candle_source"] = source
    return ok(source, pack, warnings=warnings)


@tool
@safe("fyers")
def get_option_chain_summary(symbol: str) -> dict:
    """Option-chain positioning for an F&O stock from FYERS: total call/put OI, PCR, 1-day OI change,
    top OI strikes, expiry, plus weekly/monthly OI deltas from stored snapshots. 'unavailable' when
    the stock has no listed options."""
    data = fyers.option_chain(symbol)
    return ok("fyers", fyers.summarize_option_chain(data, symbol))


# ----------------------------------------------------------------------------- fundamental
@tool
@safe("yahoo")
def get_fundamentals(symbol: str) -> dict:
    """Company profile, valuation/profitability ratios, dividend data and last quarterly results from Yahoo
    Finance. Unofficial source: cross-check material numbers with NSE filings."""
    return ok("yahoo", yahoo.fundamentals(symbol))


@tool
@safe("nse")
def get_corporate_events(symbol: str, as_of: str | None = None) -> dict:
    """NSE announcements (with PDF links), board meetings/outcomes, corporate actions (dividend, split,
    bonus...), and quarterly results for a symbol. Each event has event_id, type, dates, summary, source_url
    and published_at; events after `as_of` are removed."""
    events, warnings = nse.stock_events(symbol, parse_as_of(as_of))
    if not events:
        raise Unavailable(f"NSE returned no events for {symbol}")
    return ok("nse", {"events": events}, warnings=warnings)


@tool
@safe("google_news_rss")
def get_company_news(company_name: str, symbol: str = "", as_of: str | None = None) -> dict:
    """Recent news headlines about a company (publisher, url, publication time). Headlines are leads,
    not verified facts."""
    q = f'"{company_name}" (results OR dividend OR "board meeting" OR RBI OR NPA OR guidance OR order)'
    return ok("google_news_rss", {"items": news.search(q, _iso(as_of))})


# ----------------------------------------------------------------------------- industry / macro
@tool
@safe("rbi")
def get_rbi_policy(as_of: str | None = None) -> dict:
    """RBI repo rate (when detectable / manually maintained), recent RBI press releases and notifications
    on policy, liquidity, banking and NBFC regulation."""
    return ok("rbi", rbi.policy(_iso(as_of)))


@tool
@safe("fred+yahoo")
def get_global_rates(as_of: str | None = None) -> dict:
    """Foreign central-bank policy rates (US Fed, ECB, UK, Japan via FRED), US 10-year yield, USD/INR and
    dollar index. Context for FII flows and rate-sensitive sectors."""
    return ok("fred+yahoo", fred.global_rates(_iso(as_of)))


@tool
@safe("yahoo+google_news_rss")
def get_sector_context(symbol: str, sector_group: str, as_of: str | None = None) -> dict:
    """Sector context for a stock's sector group: stock vs sector index vs Nifty 50 returns (1m/3m) and
    recent sector news, using queries configured in config/sectors.json."""
    cfg = load_sectors().get(sector_group)
    if not cfg:
        raise Unavailable(f"no sector config for '{sector_group}' in sectors.json")
    out: dict[str, Any] = {"sector_group": sector_group, "sector_index": cfg.get("index_ticker")}
    errors = []
    try:
        out["relative_strength"] = yahoo.relative_strength(symbol, cfg["index_ticker"])
    except Exception as e:  # noqa: BLE001
        errors.append(f"relative_strength: {e}")
    sector_news: list[dict] = []
    for q in cfg.get("news_queries", [])[:3]:
        try:
            sector_news += news.search(q, _iso(as_of), days=21, limit=6)
        except (Unavailable, ProviderError) as e:
            errors.append(f"news '{q}': {e}")
    out["sector_news"] = sector_news[:15]
    out["errors"] = errors
    if "relative_strength" not in out and not sector_news:
        raise ProviderError("; ".join(errors))
    return ok("yahoo+google_news_rss", out)


# ----------------------------------------------------------------------------- flows
@tool
@safe("nse")
def get_fii_dii() -> dict:
    """Latest NSE FII/DII cash-market buy/sell/net figures. MARKET-WIDE only: never evidence about a
    single stock."""
    return ok("nse", {"scope": "market_wide", "rows": nse.fii_dii()})


@tool
@safe("nse+yahoo")
def get_stock_flows(symbol: str) -> dict:
    """Stock-SPECIFIC participation evidence: NSE bulk and block deals (90d), shareholding pattern
    snapshots, and Yahoo institutional holding %. Each part is reported as available/unavailable."""
    data: dict[str, Any] = {"scope": "stock_specific"}
    errors = []
    for name, fn in (("deals", nse.bulk_block_deals), ("shareholding", nse.shareholding)):
        try:
            data[name] = fn(symbol)
        except (ProviderError, Unavailable) as e:
            errors.append(f"{name}: {e}")
    try:
        f = yahoo.fundamentals(symbol)["profile_and_ratios"]
        data["yahoo_holdings"] = {k: f.get(k) for k in ("heldPercentInstitutions", "heldPercentInsiders")}
    except Exception as e:  # noqa: BLE001
        errors.append(f"yahoo_holdings: {e}")
    data["errors"] = errors
    if len(data) <= 2:
        raise ProviderError("; ".join(errors))
    return ok("nse+yahoo", data)
