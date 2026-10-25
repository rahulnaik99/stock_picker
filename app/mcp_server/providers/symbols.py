"""NSE symbol -> vendor symbol formats."""
from __future__ import annotations


def fyers_symbol(symbol: str) -> str:
    return f"NSE:{symbol.upper()}-EQ"


def yahoo_symbol(symbol: str) -> str:
    return f"{symbol.upper()}.NS"
