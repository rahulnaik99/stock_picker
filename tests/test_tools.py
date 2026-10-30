import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.mcp_server.tools import indicators, market, nse, rates
from app.mcp_server.tools.common import IST
from tests.fakes import synthetic_daily


# ---- indicators ---------------------------------------------------------------------------------
def trend_frame(direction: float, n=320):
    close = 500 * np.exp(np.cumsum(np.full(n, direction) + np.random.default_rng(1).normal(0, 0.004, n)))
    t = pd.date_range(end=datetime(2026, 10, 20, tzinfo=IST), periods=n, freq="B")
    return pd.DataFrame({"time": t, "open": close, "high": close * 1.006, "low": close * 0.994, "close": close, "volume": np.full(n, 2e6)})


def test_uptrend_scores_positive_and_downtrend_negative():
    up, down = indicators.compute(trend_frame(0.004)), indicators.compute(trend_frame(-0.004))
    assert up["rule_score"] > 3 and down["rule_score"] < -3
    assert -10 <= up["rule_score"] <= 10 and 0 <= up["rsi14"] <= 100


def test_indicators_need_enough_history():
    with pytest.raises(ValueError):
        indicators.compute(synthetic_daily("X", n=30))


def test_demand_supply_zones_structure_and_sides():
    z = indicators.demand_supply_zones(trend_frame(0.003))
    # zones are classified relative to price, scored, and the nearest on each side is surfaced
    assert all(zz["mid"] <= z["close"] * 1.02 for zz in z["demand_zones"])
    assert all(zz["mid"] >= z["close"] * 0.98 for zz in z["supply_zones"])
    for zz in z["demand_zones"] + z["supply_zones"]:
        assert zz["low"] <= zz["high"] and 0 <= zz["strength"] <= 100 and "confluence" in zz
    assert z["ema_stack"] in ("bullish", "bearish", "mixed")
    # a clean uptrend should read as a bullish EMA stack
    assert z["ema_stack"] == "bullish"


def test_demand_supply_zones_need_history():
    with pytest.raises(ValueError):
        indicators.demand_supply_zones(synthetic_daily("X", n=30))


def test_get_demand_supply_zones_tool_envelope(monkeypatch):
    monkeypatch.setattr(indicators, "fetch_candles",
                        lambda sym, res, days, as_of=None: (trend_frame(0.003) if res == "D" else trend_frame(0.003, n=50), "fake"))
    out = indicators.get_demand_supply_zones("SBIN")
    assert out["status"] == "ok" and out["data"]["ema_stack"] == "bullish" and "nearest_demand" in out["data"]


def test_vwap_session_only():
    t = pd.to_datetime(["2026-10-19 10:00", "2026-10-20 09:15", "2026-10-20 09:30"]).tz_localize(IST)
    df = pd.DataFrame({"time": t, "high": [200, 102, 104], "low": [200, 98, 100], "close": [200, 100, 102], "volume": [1000, 100, 300]})
    expected = (100 * 100 + 102 * 300) / 400
    assert indicators.session_vwap(df) == pytest.approx(expected)


# ---- option chain -------------------------------------------------------------------------------
CHAIN = {"s": "ok", "data": {"expiryData": [{"date": "27-10-2026"}], "optionsChain": [
    {"option_type": "", "ltp": 800.0, "ltpchp": 1.1},
    {"option_type": "CE", "strike_price": 820, "oi": 1000, "oich": 100, "volume": 50},
    {"option_type": "CE", "strike_price": 840, "oi": 3000, "oich": -50, "volume": 80},
    {"option_type": "PE", "strike_price": 780, "oi": 2500, "oich": 400, "volume": 90}]}}


def test_summarize_chain():
    s = market.summarize_chain(CHAIN, datetime.now(IST))
    assert s["total_call_oi"] == 4000 and s["total_put_oi"] == 2500 and s["pcr_oi"] == 0.625
    assert s["top_call_oi_strikes"][0]["strike"] == 840 and s["day_put_oi_change"] == 400


def test_empty_chain_raises():
    with pytest.raises(Exception):
        market.summarize_chain({"data": {"optionsChain": []}}, datetime.now(IST))


def test_option_chain_without_credentials_is_unavailable(monkeypatch):
    # Make the test independent of credentials in the developer's environment/.env.
    settings = market.get_settings()
    monkeypatch.setattr(settings, "fyers_app_id", "")
    monkeypatch.setattr(settings, "fyers_access_token", "")

    # Regression: cached data must not bypass the no-credentials contract.
    monkeypatch.setattr(market, "refreshed_today", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        market,
        "kv_get",
        lambda *args, **kwargs: {
            "summary": {"total_call_oi": 123},
            "captured_at": "2026-10-22T10:00:00+05:30",
        },
    )

    result = market.get_option_chain("SBIN")
    assert result["status"] == "unavailable"
    assert "credentials not set" in result["message"].lower()


# ---- NSE ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,kind", [("Interim Dividend declared", "dividend"), ("Outcome of Board Meeting - financial results", "results"),
                                       ("Buy-back of shares", "buyback"), ("Penalty by RBI", "regulatory_action"), ("random", "other")])
def test_classify_event(text, kind):
    assert nse.classify_event(text) == kind


def test_company_events_point_in_time(monkeypatch):
    def fake_get(path, params=None):
        if "announcements" in path:
            return [{"an_dt": "10-Oct-2026 10:00:00", "desc": "Dividend", "attchmntText": "Interim dividend", "attchmntFile": "https://nse/a.pdf", "seq_id": "1"},
                    {"an_dt": "25-Oct-2026 10:00:00", "desc": "Results", "attchmntText": "Financial results", "attchmntFile": "https://nse/b.pdf", "seq_id": "2"}]
        raise nse.SourceError("blocked")
    monkeypatch.setattr(nse, "nse_get", fake_get)
    out = nse.nse_company_events("SBIN", days=30, as_of="2026-10-20")
    assert out["status"] == "ok"
    kinds = [e["event_type"] for e in out["data"]]
    assert kinds == ["dividend"]  # the 25 Oct announcement is in the future relative to T and is excluded
    assert out["data"][0]["available_at"].startswith("2026-10-10") and len(out["feed_errors"]) == 2


def test_company_events_all_feeds_fail(monkeypatch):
    monkeypatch.setattr(nse, "nse_get", lambda *a, **k: (_ for _ in ()).throw(nse.SourceError("blocked")))
    assert nse.nse_company_events("SBIN")["status"] == "error"


def test_fii_dii_marked_market_wide(monkeypatch):
    monkeypatch.setattr(nse, "nse_get", lambda *a, **k: [{"category": "FII/FPI", "date": "20-Oct-2026", "buyValue": "1", "sellValue": "2", "netValue": "-1"}])
    assert nse.nse_fii_dii()["scope"] == "market_wide_not_stock_specific"


# ---- rates --------------------------------------------------------------------------------------
def test_foreign_rates_stale_flag(monkeypatch):
    monkeypatch.setattr(rates, "_fred_series", lambda sid, sess: [("2026-01-01", 4.0), ("2026-09-01", 3.75), ("2026-10-15", 3.5)] if sid == "DFF" else [("2025-01-01", 1.0)])
    out = rates.foreign_rates(as_of="2026-10-20")
    by = {r["series"]: r for r in out["data"]}
    assert by["DFF"]["latest"] == 3.5 and not by["DFF"]["stale"] and by["DFF"]["change_1m"] == -0.25
    assert by["ECBDFR"]["stale"]


def test_rbi_policy_flags_unverified_repo_rate(monkeypatch):
    monkeypatch.setattr(rates, "http_get", lambda *a, **k: (_ for _ in ()).throw(rates.SourceError("offline")))
    out = rates.rbi_policy(as_of="2026-10-20")
    assert out["status"] == "ok" and out["data"]["policy_rates"]["needs_verification"] is True and out["data"]["feed_notes"]


def test_policy_calendar_has_upcoming_meetings():
    rows = rates.policy_calendar(days_ahead=120, as_of="2026-10-20")["data"]
    assert any(r["category"] == "rbi_policy" for r in rows)
