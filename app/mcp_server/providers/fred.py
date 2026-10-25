"""Foreign policy rates & global drivers from FRED (free API key) + Yahoo for USD/INR and US 10y.

Series IDs live in config/sources.json ("fred_series") so you can fix/extend them without code changes.
"""
from __future__ import annotations

import json
from datetime import datetime

from app.mcp_server.providers import yahoo
from app.mcp_server.providers.common import ProviderError, Unavailable, http_get, ttl_cache
from app.service.settings import get_settings

URL = "https://api.stlouisfed.org/fred/series/observations"
DEFAULT_SERIES = {
    "us_fed_funds_target_upper": "DFEDTARU",
    "us_10y_yield": "DGS10",
    "ecb_deposit_rate": "ECBDFR",
    "uk_sonia_overnight": "IUDSOIA",
    "japan_policy_rate_monthly": "IRSTCB01JPM156N",
}


def _series_map() -> dict[str, str]:
    p = get_settings().path(get_settings().sources_file)
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("fred_series", DEFAULT_SERIES)
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_SERIES


def observations(series_id: str, as_of: datetime, n: int = 4) -> list[dict]:
    key = get_settings().fred_api_key
    if not key:
        raise Unavailable("FRED_API_KEY not set")
    body = http_get(URL, params={"series_id": series_id, "api_key": key, "file_type": "json", "sort_order": "desc",
                                 "limit": n, "observation_end": as_of.date().isoformat()}, as_json=True)
    obs = [{"date": o["date"], "value": float(o["value"])} for o in body.get("observations", []) if o.get("value") not in (".", None)]
    if not obs:
        raise Unavailable(f"FRED series {series_id} has no observations")
    return obs


@ttl_cache(3600)
def global_rates(as_of_iso: str) -> dict:
    as_of = datetime.fromisoformat(as_of_iso)
    out: dict = {"policy_rates": {}, "market": {}, "errors": {}}
    if not get_settings().fred_api_key:
        out["errors"]["fred"] = "FRED_API_KEY not set: foreign policy-rate series skipped"
    for name, sid in ({} if not get_settings().fred_api_key else _series_map()).items():
        try:
            obs = observations(sid, as_of)
            out["policy_rates"][name] = {"series": sid, "latest": obs[0], "previous": obs[1] if len(obs) > 1 else None,
                                         "change": round(obs[0]["value"] - obs[1]["value"], 3) if len(obs) > 1 else None}
        except (Unavailable, ProviderError) as e:
            out["errors"][name] = str(e)
    for name, tk in (("usd_inr", "USDINR=X"), ("us_10y_yield_yahoo", "^TNX"), ("dollar_index", "DX-Y.NYB")):
        try:
            out["market"][name] = yahoo.last_close(tk)
        except Exception as e:  # noqa: BLE001
            out["errors"][name] = str(e)
    if not out["policy_rates"] and not out["market"]:
        raise Unavailable("no global-rate data available: " + "; ".join(out["errors"].values()))
    out["note"] = "Foreign policy rates are context for FII flows/USD-INR; they are not a direct Indian-bank signal."
    return out
