"""Tests for the NSE results XBRL parser (P&L + bank KPIs, units, context selection, failure diagnostics)."""
from __future__ import annotations

from app.mcp_server.tools import xbrl

# Realistic-ish XBRL: instant contexts for NPA/CRAR; a 3-month (D_Q2) and a 9-month YTD (D_YTD) duration sharing the
# same end-date for P&L. Ratios are stored as FRACTIONS (as NSE's banking taxonomy does), amounts in raw INR.
_XBRL = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:in="http://nse.in/fin">
  <context id="I_Q1"><period><instant>2026-06-30</instant></period></context>
  <context id="I_Q2"><period><instant>2026-09-30</instant></period></context>
  <context id="D_Q2"><period><startDate>2026-07-01</startDate><endDate>2026-09-30</endDate></period></context>
  <context id="D_YTD"><period><startDate>2026-04-01</startDate><endDate>2026-09-30</endDate></period></context>
  <in:PercentageOfGrossNpa contextRef="I_Q1">0.0414</in:PercentageOfGrossNpa>
  <in:PercentageOfGrossNpa contextRef="I_Q2">0.0232</in:PercentageOfGrossNpa>
  <in:PercentageOfNetNpa contextRef="I_Q2">0.0057</in:PercentageOfNetNpa>
  <in:CapitalAdequacyRatio contextRef="I_Q2">0.1673</in:CapitalAdequacyRatio>
  <in:InterestEarned contextRef="D_Q2">285000000000</in:InterestEarned>
  <in:InterestExpended contextRef="D_Q2">174800000000</in:InterestExpended>
  <in:OtherIncome contextRef="D_Q2">60000000000</in:OtherIncome>
  <in:Income contextRef="D_Q2">345000000000</in:Income>
  <in:OperatingProfitBeforeProvisionAndContingencies contextRef="D_Q2">120000000000</in:OperatingProfitBeforeProvisionAndContingencies>
  <in:ProfitLossFromOrdinaryActivitiesBeforeTax contextRef="D_Q2">68000000000</in:ProfitLossFromOrdinaryActivitiesBeforeTax>
  <in:ProfitLossForThePeriod contextRef="D_YTD">90000000000</in:ProfitLossForThePeriod>
  <in:ProfitLossForThePeriod contextRef="D_Q2">50000000000</in:ProfitLossForThePeriod>
  <in:BasicEarningsPerShareBeforeExtraordinaryItems contextRef="D_Q2">5.93</in:BasicEarningsPerShareBeforeExtraordinaryItems>
</xbrl>"""


def test_parse_bank_kpis_pnl_kpis_units_and_context():
    d = xbrl.parse_bank_kpis(_XBRL)
    k = d["kpis"]
    assert d["status"] == "ok"                                   # gross/net NPA% + CRAR all present
    # ratios: fractions -> percent; latest quarter (2026-09-30) not the 4.14% prior quarter
    assert k["gross_npa_pct"] == 2.32 and k["net_npa_pct"] == 0.57 and k["crar_pct"] == 16.73
    assert d["matched"]["gross_npa_pct"]["context_end"] == "2026-09-30"
    # amounts -> INR crore
    assert k["total_income"] == 34500.0 and k["pbt"] == 6800.0 and k["op_profit"] == 12000.0
    assert k["nii"] == 11020.0 and k["interest_earned"] == 28500.0
    # PAT must come from the 3-month quarter (5000 cr), NOT the 9-month YTD (9000 cr)
    assert k["pat"] == 5000.0
    assert k["eps"] == 5.93


def test_parse_bank_kpis_reports_missing_when_tags_absent():
    d = xbrl.parse_bank_kpis(b'<?xml version="1.0"?><xbrl xmlns="http://x"><context id="c"><period><instant>2026-09-30</instant></period></context><SomethingElse contextRef="c">1</SomethingElse></xbrl>')
    assert d["status"] == "unavailable" and "gross_npa_pct" in d["missing"]
    assert "SomethingElse" in d["sample_tags"]


def test_parse_bank_kpis_handles_malformed_xml():
    d = xbrl.parse_bank_kpis(b"<not-xml </broken>")
    assert d["status"] == "error" and "ParseError" in d["error"]


def test_fetch_bank_kpis_never_raises_and_records_error(monkeypatch):
    def boom(url, referer=None):
        raise RuntimeError("NSE blocked the request (HTTP 403)")
    monkeypatch.setattr(xbrl.nse_tools, "nse_get_bytes", boom)
    out = xbrl.fetch_bank_kpis("https://nsearchives.nseindia.com/x.xml")
    assert out["status"] == "error" and "403" in out["error"] and out["url"].endswith("x.xml")
