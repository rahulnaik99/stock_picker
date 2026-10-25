"""Loaders for the small config files in app/config/."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.service.settings import get_settings


def _json(rel: str) -> dict[str, Any]:
    s = get_settings()
    return json.loads(s.path(rel).read_text(encoding="utf-8"))


@lru_cache
def load_weights() -> dict[str, Any]:
    cfg = _json(get_settings().weights_file)
    total = sum(cfg["weights"].values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"weights must sum to 1.0, got {total}")
    return cfg


@lru_cache
def load_sectors() -> dict[str, Any]:
    return _json(get_settings().sectors_file)


@dataclass(frozen=True)
class Stock:
    symbol: str
    company_name: str
    sector_group: str
    isin: str = ""


def load_universe(path: str | None = None, *, symbols: list[str] | None = None,
                  sector_group: str | None = None, limit: int | None = None) -> list[Stock]:
    s = get_settings()
    with s.path(path or s.universe_file).open(encoding="utf-8") as f:
        rows = [Stock(r["symbol"], r["company_name"], r["sector_group"], r.get("isin", "")) for r in csv.DictReader(f)]
    if symbols:
        wanted = {x.upper() for x in symbols}
        rows = [r for r in rows if r.symbol.upper() in wanted]
        missing = wanted - {r.symbol.upper() for r in rows}
        if missing:
            raise ValueError(f"symbols not in universe file: {sorted(missing)}")
    if sector_group:
        rows = [r for r in rows if r.sector_group == sector_group]
    return rows[:limit] if limit else rows
