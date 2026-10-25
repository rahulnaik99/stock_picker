"""Shared helpers for every tool: result envelope, time handling, HTTP with retries."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def now_ist() -> datetime:
    return datetime.now(IST)


def now_iso() -> str:
    return now_ist().isoformat(timespec="seconds")


# --- result envelope: every tool returns one of these, never raises ---------------------------
def ok(source: str, data: Any, **extra: Any) -> dict[str, Any]:
    return {"status": "ok", "source": source, "retrieved_at": now_iso(), "data": data, **extra}


def unavailable(source: str, message: str, **extra: Any) -> dict[str, Any]:
    """Data does not exist / not permitted / not applicable. NOT the same as a neutral value."""
    return {"status": "unavailable", "source": source, "retrieved_at": now_iso(), "message": message, "data": None, **extra}


def error(source: str, exc: BaseException | str, **extra: Any) -> dict[str, Any]:
    msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return {"status": "error", "source": source, "retrieved_at": now_iso(), "message": msg[:500], "data": None, **extra}


class SourceError(RuntimeError):
    """Raised inside tools; the registry wrapper converts it to an error envelope."""


# --- time -----------------------------------------------------------------------------------
def parse_as_of(as_of: str | None) -> datetime:
    """as_of = analysis time T. None -> now. A bare date means end of that day (IST)."""
    if not as_of:
        return now_ist()
    text = as_of.strip()
    if len(text) == 10:
        d = date.fromisoformat(text)
        return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=IST)
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def parse_any_dt(value: Any) -> datetime | None:
    """Parse the date formats NSE/RSS use. Returns tz-aware IST datetime or None."""
    if value in (None, "", "-"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    text = str(value).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%y", "%Y-%m-%dT%H:%M:%S", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    except ValueError:
        return None


def window(as_of: datetime, days: int) -> tuple[datetime, datetime]:
    return as_of - timedelta(days=days), as_of


# --- HTTP -----------------------------------------------------------------------------------
def http_get(session: requests.Session, url: str, *, params: dict | None = None, headers: dict | None = None,
             timeout: float = 20.0, retries: int = 2, backoff: float = 0.8) -> requests.Response:
    """GET with retry on network errors / 429 / 5xx. Auth failures (401/403) are not retried."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            return resp
        except requests.RequestException as exc:
            last = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise SourceError(f"network error for {url}: {type(last).__name__}")


def clean_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    for prefix in ("NSE:", "BSE:"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    for suffix in ("-EQ", ".NS", ".BO"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def fyers_symbol(symbol: str) -> str:
    return f"NSE:{clean_symbol(symbol)}-EQ"


def yahoo_symbol(symbol: str) -> str:
    return f"{clean_symbol(symbol)}.NS"
