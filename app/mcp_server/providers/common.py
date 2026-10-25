"""Shared helpers for all data providers: result envelope, errors, HTTP with retries, TTL cache."""
from __future__ import annotations

import functools
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from app.service.logger import get_logger, short
from app.service.settings import get_settings

log = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


class Unavailable(Exception):
    """Expected: not configured / not applicable (e.g. stock has no options). Reported as 'unavailable'."""


class ProviderError(Exception):
    """Unexpected provider failure (auth, throttling, bad payload). Reported as 'error'."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_as_of(value: str | None) -> datetime:
    """ISO string -> aware datetime (IST if naive). None -> now."""
    if not value:
        return datetime.now(IST)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def envelope(status: str, source: str, *, data: Any = None, reason: str | None = None, **extra: Any) -> dict:
    out = {"status": status, "source": source, "retrieved_at": now_iso()}
    if reason:
        out["reason"] = reason
    if data is not None:
        out["data"] = data
    out.update(extra)
    return out


def ok(source: str, data: Any, **extra: Any) -> dict:
    return envelope("ok", source, data=data, **extra)


def safe(source: str) -> Callable:
    """Decorator: a tool never raises; failures become explicit 'unavailable' / 'error' envelopes."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*a: Any, **k: Any) -> dict:
            t0 = time.monotonic()
            try:
                res = fn(*a, **k)
                log.info("tool %s ok (%.1fs) status=%s", fn.__name__, time.monotonic() - t0, res.get("status"))
                return res
            except Unavailable as e:
                log.info("tool %s unavailable: %s", fn.__name__, e)
                return envelope("unavailable", source, reason=str(e))
            except Exception as e:  # noqa: BLE001
                log.warning("tool %s failed: %s: %s", fn.__name__, type(e).__name__, short(str(e), 300))
                return envelope("error", source, reason=f"{type(e).__name__}: {e}")
        return wrapper
    return deco


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
             client: httpx.Client | None = None, as_json: bool = False) -> Any:
    """GET with retry/backoff on timeouts, 429 and 5xx. Raises ProviderError when exhausted."""
    s = get_settings()
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", **(headers or {})}
    last: Exception | None = None
    for attempt in range(max(1, s.http_retries)):
        try:
            c = client or httpx.Client(timeout=s.http_timeout, follow_redirects=True)
            try:
                r = c.get(url, params=params, headers=hdrs)
            finally:
                if client is None:
                    c.close()
            if r.status_code in (429, 500, 502, 503, 504):
                raise ProviderError(f"HTTP {r.status_code} from {url}")
            if r.status_code >= 400:
                raise ProviderError(f"HTTP {r.status_code} from {url}: {short(r.text, 200)}")
            return r.json() if as_json else r.text
        except (httpx.HTTPError, ProviderError, ValueError) as e:
            last = e
            if isinstance(e, ProviderError) and "HTTP 4" in str(e) and "429" not in str(e):
                break  # client error: retrying will not help
            time.sleep(0.8 * (2 ** attempt))
    raise ProviderError(f"GET {url} failed: {last}")


_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def ttl_cache(seconds: float) -> Callable:
    """Cache successful results in-process (the MCP server is shared by all agents/stocks)."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*a: Any, **k: Any) -> Any:
            key = (fn.__module__, fn.__name__, a, tuple(sorted(k.items())))
            now = time.monotonic()
            with _cache_lock:
                hit = _cache.get(key)
                if hit and now - hit[0] < seconds:
                    return hit[1]
            val = fn(*a, **k)
            with _cache_lock:
                _cache[key] = (now, val)
            return val
        return wrapper
    return deco


def first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if d.get(k) not in (None, "", "-"):
            return d[k]
    return default
