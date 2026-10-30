"""Regenerate SOURCES_CATALOG.md from app/config/universe.csv.   python -m scripts.build_catalog"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]

SHARED = [
    ("Domestic policy", "RBI home / policy rates", "https://www.rbi.org.in/", "Repo rate, MPC statements, press releases"),
    ("Domestic policy", "RBI press releases (RSS)", "https://rbi.org.in/pressreleases_rss.xml", "Used by the rbi_policy tool"),
    ("Domestic policy", "RBI notifications (RSS)", "https://rbi.org.in/notifications_rss.xml", "Directions, penalties, circulars. Used by rbi_policy"),
    ("Domestic policy", "RBI Financial Stability Report", "https://www.rbi.org.in/Scripts/FsReport.aspx", "Sector asset quality, stress tests (PDF, half-yearly)"),
    ("Domestic policy", "RBI DBIE database", "https://dbie.rbi.org.in/", "Credit/deposit growth, sectoral deployment of credit"),
    ("Domestic policy", "MPC calendar", "app/config/macro_events.csv", "Static; confirm on rbi.org.in"),
    ("Regulators", "SEBI circulars", "https://www.sebi.gov.in/", "Brokers, AMCs, exchanges, F&O rules"),
    ("Regulators", "IRDAI", "https://irdai.gov.in/", "Insurance regulation and monthly premium data"),
    ("Regulators", "NHB", "https://nhb.org.in/", "Housing finance regulation"),
    ("Regulators", "AMFI (SIP flows, AUM)", "https://www.amfiindia.com/", "Monthly industry flows for AMCs"),
    ("Regulators", "NPCI (UPI statistics)", "https://www.npci.org.in/", "Payments volumes for fintech names"),
    ("Exchange filings", "NSE corporate announcements", "https://www.nseindia.com/companies-listing/corporate-filings-announcements", "Per symbol; the nse_company_events tool"),
    ("Exchange filings", "NSE board meetings", "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings", "Dates and purpose (results, dividend, fund raise)"),
    ("Exchange filings", "NSE corporate actions", "https://www.nseindia.com/companies-listing/corporate-filings-actions", "Dividend, split, bonus, rights, buyback, ex-dates"),
    ("Exchange filings", "NSE financial results", "https://www.nseindia.com/companies-listing/corporate-filings-financial-results", "Quarterly/annual results (XBRL and PDF)"),
    ("Exchange filings", "NSE shareholding pattern", "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern", "Quarterly promoter/FPI/DII holdings"),
    ("Exchange filings", "NSE event calendar", "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar", "Upcoming results and meetings"),
    ("Exchange filings", "BSE corporate announcements", "https://www.bseindia.com/corporates/ann.html", "Fallback to NSE"),
    ("Flows", "NSE FII/DII report", "https://www.nseindia.com/reports/fii-dii", "Market-wide. Used by nse_fii_dii"),
    ("Flows", "NSE bulk and block deals", "https://www.nseindia.com/report-detail/display-bulk-and-block-deals", "Stock-specific. Used by nse_deals"),
    ("Flows", "NSDL FPI monitor", "https://www.fpi.nsdl.co.in/", "FPI flows by sector (fortnightly)"),
    ("Index data", "NSE Indices (Nifty Bank / Financial Services)", "https://www.niftyindices.com/", "Constituents, factsheets, weights"),
    ("Foreign rates", "US FOMC calendar", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "In macro_events.csv"),
    ("Foreign rates", "FRED: Fed funds (DFF)", "https://fred.stlouisfed.org/series/DFF", "Used by foreign_rates (keyless CSV)"),
    ("Foreign rates", "FRED: US 10Y (DGS10)", "https://fred.stlouisfed.org/series/DGS10", "Used by foreign_rates"),
    ("Foreign rates", "ECB decisions", "https://www.ecb.europa.eu/mopo/decisions/html/index.en.html", "FRED series ECBDFR"),
    ("Foreign rates", "Bank of England", "https://www.bankofengland.co.uk/monetary-policy", "Bank Rate decisions"),
    ("Foreign rates", "Bank of Japan", "https://www.boj.or.jp/en/mopo/index.htm", "Policy rate decisions"),
    ("Markets", "FBIL benchmarks / CCIL", "https://www.fbil.org.in/", "Reference rates, G-sec yields"),
    ("Ratings", "CRISIL / ICRA ratings", "https://www.icra.in/", "Rating actions on lenders (also surface as NSE announcements)"),
]

FETCH = {
    "bank_private": "NII, NIM, GNPA/NNPA, provision coverage (PCR), slippages, CASA ratio, advances and deposit growth, CRAR/CET1, ROA/ROE, LCR; Pillar 3 disclosures",
    "bank_small_finance": "NIM, GNPA/NNPA, microfinance and unsecured book share, collection efficiency, CASA, cost of funds, CRAR",
    "bank_psu": "NII, NIM, GNPA/NNPA, PCR, slippages, recoveries, treasury income, credit growth, CRAR, government recapitalisation or stake-sale news",
    "housing_finance": "AUM, disbursements, spread, cost of funds, GNPA (Stage 3), ALM, credit rating, NHB rules",
    "nbfc": "AUM growth, disbursements, NIM/spread, cost of funds, Stage 2/3 assets, credit cost, collection efficiency, liquidity and borrowing mix, rating actions",
    "govt_financier": "Loan book, sanctions/disbursements, Stage 3, funding cost, dividend payout, government policy",
    "holding_investment": "Subsidiary results (lending, insurance), holding-company discount, dividends received",
    "insurance_life": "APE, new business premium, VNB and VNB margin, persistency, solvency ratio, AUM, product mix; IRDAI monthly data",
    "insurance_general": "Gross written premium, combined ratio, claims ratio, solvency, investment yield; IRDAI monthly data",
    "amc_wealth": "QAAUM, equity AUM share, SIP flows, yields, market share (AMFI), operating margin",
    "broker_exchange_infra": "Turnover/ADTV, F&O volumes, active clients and demat additions, market share, revenue mix, SEBI rule changes",
    "fintech_payments": "GMV, take rate, user growth, lending partner/credit losses, regulatory actions (RBI/NPCI)",
}
PER_COMPANY = ["Quarterly results (NSE financial results + results PDF)", "Investor presentation and earnings-call transcript (company IR / NSE announcements)",
               "Annual report (company IR)", "Board-meeting outcomes and corporate actions (NSE)", "Shareholding pattern (NSE)",
               "Credit-rating letters (NSE announcements)", "Banks: Basel III Pillar 3 disclosure and quarterly NPA disclosures (company IR)"]


def main() -> None:
    rows = list(csv.DictReader(open(ROOT / "app/config/universe.csv")))
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["sub_sector"]].append(r)
    L = ["# Sources catalog: financial and banking stocks", "",
         f"Universe: {len(rows)} NSE financial-services stocks (from the Nifty 500 constituent file, snapshot 2026-10-10), grouped by sub-sector. "
         "Regenerate with `python -m scripts.build_catalog`.", "",
         "> **Verify before relying.** URLs are the standard public pages. Exchange and regulator sites change layouts and block automated access; "
         "the app treats a failed fetch as `unavailable`, never as neutral. Per-company investor-relations (IR) page URLs are not included because they "
         "could not be verified from here: search `<company name> investor relations` once and add them to `app/config/universe.csv` if you want them tracked.", "",
         "## 1. Shared sources", "", "| Area | Source | URL | Use |", "|---|---|---|---|"]
    L += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in SHARED]
    L += ["", "## 2. Documents to analyse for every company", ""] + [f"- {x}" for x in PER_COMPANY]
    L += ["", "## 3. What to fetch per sub-sector", "", "| Sub-sector | Stocks | Key metrics and documents |", "|---|---|---|"]
    for sub, items in groups.items():
        L.append(f"| `{sub}` | {len(items)} | {FETCH.get(sub, '')} |")
    L += ["", "## 4. Stocks (with per-stock pages)", "",
          "NSE quote pages carry the announcements, results and shareholding tabs for each symbol.", ""]
    for sub, items in groups.items():
        L += [f"### {sub} ({len(items)})", "", "| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |", "|---|---|---|---|"]
        for r in sorted(items, key=lambda x: x["symbol"]):
            s = r["symbol"]
            L.append(f"| {s} | {r['company_name']} | https://www.nseindia.com/get-quotes/equity?symbol={quote(s)} | https://www.screener.in/company/{quote(s)}/consolidated/ |")
        L.append("")
    (ROOT / "SOURCES_CATALOG.md").write_text("\n".join(L), encoding="utf-8")
    print("wrote SOURCES_CATALOG.md", len(L), "lines")


if __name__ == "__main__":
    main()
