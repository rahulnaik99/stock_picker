"""Shared helpers for tools: result envelope, resilient HTTP, rate limiter, time helpers.

Every tool returns the same envelope so agents and scoring can treat failures uniformly:
  {"status": "ok|partial|stale|unavailable|error", "source": str, "retrieved_at": iso, "as_of": str, "data": ..., "error": str|None}
Tools never raise to the agent and never invent data: failures become status="unavailable"/"error".
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from app.service.logger import get_logger

log = get_logger("tools")
IST = timezone(timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def envelope(status: str, source: str, data: Any = None, error: str | None = None, as_of: str = "", **extra) -> dict:
    out = {"status": status, "source": source, "retrieved_at": now_iso(), "as_of": as_of, "data": data, "error": error}
    out.update(extra)
    return out


def unavailable(source: str, reason: str, as_of: str = "") -> dict:
    log.warning("unavailable source=%s reason=%s", source, reason)
    return envelope("unavailable", source, None, reason, as_of)


def parse_as_of(as_of: str | None) -> datetime:
    """as_of is an ISO date/datetime; empty means now. Returned timezone-aware (IST if naive)."""
    if not as_of:
        return datetime.now(IST)
    dt = datetime.fromisoformat(as_of)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


class RateLimiter:
    """Thread-safe sliding window limiter (per second and per minute)."""

    def __init__(self, per_second: int, per_minute: int):
        self.per_second, self.per_minute = per_second, per_minute
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._hits and now - self._hits[0] > 60:
                    self._hits.popleft()
                last_sec = sum(1 for t in self._hits if now - t <= 1)
                if last_sec < self.per_second and len(self._hits) < self.per_minute:
                    self._hits.append(now)
                    return
            time.sleep(0.2)


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None, session: requests.Session | None = None,
             timeout: float = 15, retries: int = 2, backoff: float = 1.0, as_json: bool = True):
    """GET with retry on network errors / 429 / 5xx. Raises RuntimeError with a short message on final failure."""
    sess = session or requests
    last = "unknown error"
    for attempt in range(retries + 1):
        try:
            resp = sess.get(url, params=params, headers={"User-Agent": UA, **(headers or {})}, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {resp.status_code}"
            elif resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code} from {url}")
            else:
                return resp.json() if as_json else resp.text
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        except ValueError as exc:  # bad JSON
            raise RuntimeError(f"Invalid response body from {url}: {exc}") from exc
        time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"{last} ({url})")


class TTLCache:
    def __init__(self, ttl: float):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
        return None

    def set(self, key, value):
        with self._lock:
            self._d[key] = (time.monotonic(), value)


def require_current(as_of: str | None, source: str, max_age_days: int = 3) -> str | None:
    """Return an 'unavailable' reason when a live-snapshot source is asked for a past date (no point-in-time data)."""
    if not as_of:
        return None
    if (datetime.now(IST) - parse_as_of(as_of)).days > max_age_days:
        return f"{source} only provides current snapshots; it cannot be used for as_of={as_of} (look-ahead risk)"
    return None
