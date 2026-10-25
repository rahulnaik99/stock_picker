"""Tests for the NSE results XBRL bank-KPI parser (pure parse + fetch-failure diagnostics)."""
from __future__ import annotations

from app.mcp_server.tools import xbrl

# Minimal XBRL-like doc: two contexts (prior quarter 30-Jun, current 30-Sep). The parser must pick the LATEST.
_XBRL = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:in="http://nse.in/fin">
  <context id="C_Q1"><period><instant>2026-06-30</instant></period></context>
  <context id="C_Q2"><period><instant>2026-09-30</instant></period></context>
  <context id="D_Q2"><period><startDate>2026-07-01</startDate><endDate>2026-09-30</endDate></period></context>
  <in:PercentageOfGrossNpa contextRef="C_Q1">4.14</in:PercentageOfGrossNpa>
  <in:PercentageOfGrossNpa contextRef="C_Q2">2.32</in:PercentageOfGrossNpa>
  <in:PercentageOfNetNpa contextRef="C_Q2">0.57</in:PercentageOfNetNpa>
  <in:CapitalAdequacyRatio contextRef="C_Q2">16.73</in:CapitalAdequacyRatio>
  <in:InterestEarned contextRef="D_Q2">2850000</in:InterestEarned>
  <in:InterestExpended contextRef="D_Q2">1748000</in:InterestExpended>
  <in:OtherIncome contextRef="D_Q2">600000</in:OtherIncome>
</xbrl>"""


def test_parse_bank_kpis_picks_latest_context_and_derives_nii():
    d = xbrl.parse_bank_kpis(_XBRL)
    assert d["status"] == "ok"                                  # all 3 core KPIs present
    assert d["kpis"]["gross_npa_pct"] == 2.32                   # current quarter, not the 4.14 prior quarter
    assert d["kpis"]["net_npa_pct"] == 0.57 and d["kpis"]["crar_pct"] == 16.73
    assert d["matched"]["gross_npa_pct"]["context_end"] == "2026-09-30"
    assert d["kpis"]["nii"] == 1102000.0                        # 2,850,000 - 1,748,000 (derived)
    assert d["facts_parsed"] >= 7 and d["sample_tags"]


def test_parse_bank_kpis_reports_missing_when_tags_absent():
    d = xbrl.parse_bank_kpis(b'<?xml version="1.0"?><xbrl xmlns="http://x"><context id="c"><period><instant>2026-09-30</instant></period></context><SomethingElse contextRef="c">1</SomethingElse></xbrl>')
    assert d["status"] == "unavailable" and "gross_npa_pct" in d["missing"]
    assert "SomethingElse" in d["sample_tags"]                  # so a failed match is debuggable from the report


def test_parse_bank_kpis_handles_malformed_xml():
    d = xbrl.parse_bank_kpis(b"<not-xml </broken>")
    assert d["status"] == "error" and "ParseError" in d["error"]


def test_fetch_bank_kpis_never_raises_and_records_error(monkeypatch):
    def boom(url, referer=None):
        raise RuntimeError("NSE blocked the request (HTTP 403)")
    monkeypatch.setattr(xbrl.nse_tools, "nse_get_bytes", boom)
    out = xbrl.fetch_bank_kpis("https://nsearchives.nseindia.com/x.xml")
    assert out["status"] == "error" and "403" in out["error"] and out["url"].endswith("x.xml")
