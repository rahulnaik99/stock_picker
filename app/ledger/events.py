"""Append/upsert event ledger (CSV). Never overwrites history; point-in-time reads via available_at.

Single-writer safe: all writes take a file lock. Migrate to a DB if volume or concurrency grows.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from filelock import FileLock

from app.service.settings import get_settings

COLUMNS = ["event_id", "symbol", "exchange", "event_type", "announcement_date", "event_date", "event_status", "event_summary",
           "source_url", "published_at", "retrieved_at", "available_at", "event_impact_score", "analyst_reasoning", "last_updated"]


class EventLedger:
    def __init__(self, path: Path | None = None):
        s = get_settings()
        self.path = path or s.path(s.events_csv)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self.path) + ".lock", timeout=30)

    def _read(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=COLUMNS)
        return pd.read_csv(self.path, dtype=str, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")

    def upsert(self, events: list[dict[str, Any]]) -> tuple[int, int]:
        """Insert new events, update known ones. First-seen retrieved_at/available_at and any impact score are preserved."""
        if not events:
            return 0, 0
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            df = self._read().set_index("event_id", drop=False)
            new = upd = 0
            for ev in events:
                row = {c: str(ev.get(c, "") or "") for c in COLUMNS}
                eid = row["event_id"]
                if eid in df.index:
                    old = df.loc[eid]
                    for keep in ("retrieved_at", "available_at", "event_impact_score", "analyst_reasoning"):
                        if old[keep] and (keep in ("retrieved_at", "available_at") or not row[keep]):
                            row[keep] = old[keep]
                    if any(str(old[c]) != row[c] for c in ("event_status", "event_summary", "event_date")):
                        upd += 1
                    row["last_updated"] = stamp if any(str(old[c]) != row[c] for c in COLUMNS[:-1]) else old["last_updated"]
                    df.loc[eid] = pd.Series(row)
                else:
                    row["last_updated"] = stamp
                    df.loc[eid] = pd.Series(row)
                    new += 1
            df.reset_index(drop=True).to_csv(self.path, index=False)
        return new, upd

    def query(self, symbol: str, as_of: datetime, lookback_days: int = 120, lookahead_days: int = 90) -> list[dict[str, Any]]:
        """Events for a symbol that were demonstrably available by `as_of`, newest first."""
        df = self._read()
        df = df[df["symbol"] == symbol]
        if df.empty:
            return []
        avail = pd.to_datetime(df["available_at"], errors="coerce", utc=True)
        t = pd.Timestamp(as_of).tz_convert("UTC")
        df = df[avail <= t]
        ev = pd.to_datetime(df["event_date"], errors="coerce")
        lo, hi = (t - pd.Timedelta(days=lookback_days)).tz_localize(None), (t + pd.Timedelta(days=lookahead_days)).tz_localize(None)
        df = df[(ev >= lo) & (ev <= hi) | ev.isna()]
        return df.sort_values("available_at", ascending=False).to_dict("records")

    def update_impacts(self, items: list[dict[str, Any]]) -> int:
        """Store the Fundamental analyst's per-event impact score and reasoning."""
        if not items:
            return 0
        with self._lock:
            df = self._read().set_index("event_id", drop=False)
            n = 0
            for it in items:
                eid = str(it.get("event_id", ""))
                if eid in df.index:
                    df.at[eid, "event_impact_score"] = str(it.get("impact_score", ""))
                    df.at[eid, "analyst_reasoning"] = str(it.get("reasoning", ""))[:500]
                    n += 1
            df.reset_index(drop=True).to_csv(self.path, index=False)
        return n

    def all_for(self, symbols: list[str]) -> pd.DataFrame:
        df = self._read()
        return df[df["symbol"].isin(symbols)].sort_values(["symbol", "available_at"], ascending=[True, False])
