"""Minimal read-only FYERS v3 data client (history + option chain). Orders are NOT implemented on purpose."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.service.settings import get_settings
from app.tools.common import IST, RateLimiter, http_get

HISTORY_URL = "https://api-t1.fyers.in/data/history"
OPTION_CHAIN_URL = "https://api-t1.fyers.in/data/options-chain-v3"
_limiter = RateLimiter(per_second=4, per_minute=45)  # stay under the Standard plan (5/s, 50/min)


class FyersNotConfigured(RuntimeError):
    pass


def fyers_symbol(nse_symbol: str) -> str:
    return f"NSE:{nse_symbol}-EQ"


def _headers() -> dict:
    s = get_settings()
    if not (s.fyers_app_id and s.fyers_access_token):
        raise FyersNotConfigured("FYERS_APP_ID / FYERS_ACCESS_TOKEN not set (token expires daily: run scripts/fyers_login.py)")
    return {"Authorization": f"{s.fyers_app_id}:{s.fyers_access_token}"}


def _get(url: str, params: dict) -> dict:
    _limiter.wait()
    body = http_get(url, params=params, headers=_headers())
    if body.get("s") != "ok":
        msg = body.get("message") or body
        if body.get("code") in (-16, -15, -300, 401, 403) or "token" in str(msg).lower():
            raise RuntimeError(f"FYERS authentication/permission error: {msg}")
        raise RuntimeError(f"FYERS error: {msg}")
    return body


def history(nse_symbol: str, resolution: str, start: datetime, end: datetime) -> list[list[float]]:
    """Candles [epoch, o, h, l, c, v]. Chunked: <=366 days for daily, <=100 days for intraday."""
    step = 360 if resolution in ("D", "1D") else 95
    rows: list[list[float]] = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=step), end)
        body = _get(HISTORY_URL, {"symbol": fyers_symbol(nse_symbol), "resolution": resolution, "date_format": "1",
                                  "range_from": cur.strftime("%Y-%m-%d"), "range_to": nxt.strftime("%Y-%m-%d"), "cont_flag": "1"})
        rows.extend(body.get("candles") or [])
        cur = nxt + timedelta(days=1)
    return rows


def option_chain(nse_symbol: str, strikecount: int = 10) -> dict:
    return _get(OPTION_CHAIN_URL, {"symbol": fyers_symbol(nse_symbol), "strikecount": strikecount}).get("data", {})


def today_ist() -> datetime:
    return datetime.now(IST)
