# Sources catalog: financial and banking stocks

Universe: 98 NSE financial-services stocks (from the Nifty 500 constituent file, snapshot 2026-10-10), grouped by sub-sector. Regenerate with `python -m scripts.build_catalog`.

> **Verify before relying.** URLs are the standard public pages. Exchange and regulator sites change layouts and block automated access; the app treats a failed fetch as `unavailable`, never as neutral. Per-company investor-relations (IR) page URLs are not included because they could not be verified from here: search `<company name> investor relations` once and add them to `app/config/universe.csv` if you want them tracked.

## 1. Shared sources

| Area | Source | URL | Use |
|---|---|---|---|
| Domestic policy | RBI home / policy rates | https://www.rbi.org.in/ | Repo rate, MPC statements, press releases |
| Domestic policy | RBI press releases (RSS) | https://rbi.org.in/pressreleases_rss.xml | Used by the rbi_policy tool |
| Domestic policy | RBI notifications (RSS) | https://rbi.org.in/notifications_rss.xml | Directions, penalties, circulars. Used by rbi_policy |
| Domestic policy | RBI Financial Stability Report | https://www.rbi.org.in/Scripts/FsReport.aspx | Sector asset quality, stress tests (PDF, half-yearly) |
| Domestic policy | RBI DBIE database | https://dbie.rbi.org.in/ | Credit/deposit growth, sectoral deployment of credit |
| Domestic policy | MPC calendar | app/config/macro_events.csv | Static; confirm on rbi.org.in |
| Regulators | SEBI circulars | https://www.sebi.gov.in/ | Brokers, AMCs, exchanges, F&O rules |
| Regulators | IRDAI | https://irdai.gov.in/ | Insurance regulation and monthly premium data |
| Regulators | NHB | https://nhb.org.in/ | Housing finance regulation |
| Regulators | AMFI (SIP flows, AUM) | https://www.amfiindia.com/ | Monthly industry flows for AMCs |
| Regulators | NPCI (UPI statistics) | https://www.npci.org.in/ | Payments volumes for fintech names |
| Exchange filings | NSE corporate announcements | https://www.nseindia.com/companies-listing/corporate-filings-announcements | Per symbol; the nse_company_events tool |
| Exchange filings | NSE board meetings | https://www.nseindia.com/companies-listing/corporate-filings-board-meetings | Dates and purpose (results, dividend, fund raise) |
| Exchange filings | NSE corporate actions | https://www.nseindia.com/companies-listing/corporate-filings-actions | Dividend, split, bonus, rights, buyback, ex-dates |
| Exchange filings | NSE financial results | https://www.nseindia.com/companies-listing/corporate-filings-financial-results | Quarterly/annual results (XBRL and PDF) |
| Exchange filings | NSE shareholding pattern | https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern | Quarterly promoter/FPI/DII holdings |
| Exchange filings | NSE event calendar | https://www.nseindia.com/companies-listing/corporate-filings-event-calendar | Upcoming results and meetings |
| Exchange filings | BSE corporate announcements | https://www.bseindia.com/corporates/ann.html | Fallback to NSE |
| Flows | NSE FII/DII report | https://www.nseindia.com/reports/fii-dii | Market-wide. Used by nse_fii_dii |
| Flows | NSE bulk and block deals | https://www.nseindia.com/report-detail/display-bulk-and-block-deals | Stock-specific. Used by nse_deals |
| Flows | NSDL FPI monitor | https://www.fpi.nsdl.co.in/ | FPI flows by sector (fortnightly) |
| Index data | NSE Indices (Nifty Bank / Financial Services) | https://www.niftyindices.com/ | Constituents, factsheets, weights |
| Foreign rates | US FOMC calendar | https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm | In macro_events.csv |
| Foreign rates | FRED: Fed funds (DFF) | https://fred.stlouisfed.org/series/DFF | Used by foreign_rates (keyless CSV) |
| Foreign rates | FRED: US 10Y (DGS10) | https://fred.stlouisfed.org/series/DGS10 | Used by foreign_rates |
| Foreign rates | ECB decisions | https://www.ecb.europa.eu/mopo/decisions/html/index.en.html | FRED series ECBDFR |
| Foreign rates | Bank of England | https://www.bankofengland.co.uk/monetary-policy | Bank Rate decisions |
| Foreign rates | Bank of Japan | https://www.boj.or.jp/en/mopo/index.htm | Policy rate decisions |
| Markets | FBIL benchmarks / CCIL | https://www.fbil.org.in/ | Reference rates, G-sec yields |
| Ratings | CRISIL / ICRA ratings | https://www.icra.in/ | Rating actions on lenders (also surface as NSE announcements) |

## 2. Documents to analyse for every company

- Quarterly results (NSE financial results + results PDF)
- Investor presentation and earnings-call transcript (company IR / NSE announcements)
- Annual report (company IR)
- Board-meeting outcomes and corporate actions (NSE)
- Shareholding pattern (NSE)
- Credit-rating letters (NSE announcements)
- Banks: Basel III Pillar 3 disclosure and quarterly NPA disclosures (company IR)

## 3. What to fetch per sub-sector

| Sub-sector | Stocks | Key metrics and documents |
|---|---|---|
| `amc_wealth` | 8 | QAAUM, equity AUM share, SIP flows, yields, market share (AMFI), operating margin |
| `bank_small_finance` | 1 | NIM, GNPA/NNPA, microfinance and unsecured book share, collection efficiency, CASA, cost of funds, CRAR |
| `housing_finance` | 8 | AUM, disbursements, spread, cost of funds, GNPA (Stage 3), ALM, credit rating, NHB rules |
| `nbfc` | 20 | AUM growth, disbursements, NIM/spread, cost of funds, Stage 2/3 assets, credit cost, collection efficiency, liquidity and borrowing mix, rating actions |
| `broker_exchange_infra` | 11 | Turnover/ADTV, F&O volumes, active clients and demat additions, market share, revenue mix, SEBI rule changes |
| `holding_investment` | 6 | Subsidiary results (lending, insurance), holding-company discount, dividends received |
| `bank_private` | 13 | NII, NIM, GNPA/NNPA, provision coverage (PCR), slippages, CASA ratio, advances and deposit growth, CRAR/CET1, ROA/ROE, LCR; Pillar 3 disclosures |
| `bank_psu` | 12 | NII, NIM, GNPA/NNPA, PCR, slippages, recoveries, treasury income, credit growth, CRAR, government recapitalisation or stake-sale news |
| `insurance_life` | 6 | APE, new business premium, VNB and VNB margin, persistency, solvency ratio, AUM, product mix; IRDAI monthly data |
| `insurance_general` | 4 | Gross written premium, combined ratio, claims ratio, solvency, investment yield; IRDAI monthly data |
| `govt_financier` | 6 | Loan book, sanctions/disbursements, Stage 3, funding cost, dividend payout, government policy |
| `fintech_payments` | 3 | GMV, take rate, user growth, lending partner/credit losses, regulatory actions (RBI/NPCI) |

## 4. Stocks (with per-stock pages)

NSE quote pages carry the announcements, results and shareholding tabs for each symbol.

### amc_wealth (8)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| 360ONE | 360 ONE WAM Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=360ONE | https://www.screener.in/company/360ONE/consolidated/ |
| ABSLAMC | Aditya Birla Sun Life AMC Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=ABSLAMC | https://www.screener.in/company/ABSLAMC/consolidated/ |
| ANANDRATHI | Anand Rathi Wealth Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=ANANDRATHI | https://www.screener.in/company/ANANDRATHI/consolidated/ |
| HDFCAMC | HDFC Asset Management Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=HDFCAMC | https://www.screener.in/company/HDFCAMC/consolidated/ |
| ICICIAMC | ICICI Prudential Asset Management Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=ICICIAMC | https://www.screener.in/company/ICICIAMC/consolidated/ |
| NAM-INDIA | Nippon Life India Asset Management Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=NAM-INDIA | https://www.screener.in/company/NAM-INDIA/consolidated/ |
| NUVAMA | Nuvama Wealth Management Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=NUVAMA | https://www.screener.in/company/NUVAMA/consolidated/ |
| UTIAMC | UTI Asset Management Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=UTIAMC | https://www.screener.in/company/UTIAMC/consolidated/ |

### bank_small_finance (1)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| AUBANK | AU Small Finance Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=AUBANK | https://www.screener.in/company/AUBANK/consolidated/ |

### housing_finance (8)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| AADHARHFC | Aadhar Housing Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=AADHARHFC | https://www.screener.in/company/AADHARHFC/consolidated/ |
| AAVAS | Aavas Financiers Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=AAVAS | https://www.screener.in/company/AAVAS/consolidated/ |
| APTUS | Aptus Value Housing Finance India Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=APTUS | https://www.screener.in/company/APTUS/consolidated/ |
| BAJAJHFL | Bajaj Housing Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=BAJAJHFL | https://www.screener.in/company/BAJAJHFL/consolidated/ |
| CANFINHOME | Can Fin Homes Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CANFINHOME | https://www.screener.in/company/CANFINHOME/consolidated/ |
| HOMEFIRST | Home First Finance Company India Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=HOMEFIRST | https://www.screener.in/company/HOMEFIRST/consolidated/ |
| LICHSGFIN | LIC Housing Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=LICHSGFIN | https://www.screener.in/company/LICHSGFIN/consolidated/ |
| PNBHOUSING | PNB Housing Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=PNBHOUSING | https://www.screener.in/company/PNBHOUSING/consolidated/ |

### nbfc (20)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| ABCAPITAL | Aditya Birla Capital Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=ABCAPITAL | https://www.screener.in/company/ABCAPITAL/consolidated/ |
| BAJFINANCE | Bajaj Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=BAJFINANCE | https://www.screener.in/company/BAJFINANCE/consolidated/ |
| CGCL | Capri Global Capital Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CGCL | https://www.screener.in/company/CGCL/consolidated/ |
| CHOLAFIN | Cholamandalam Investment and Finance Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CHOLAFIN | https://www.screener.in/company/CHOLAFIN/consolidated/ |
| CREDITACC | CreditAccess Grameen Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CREDITACC | https://www.screener.in/company/CREDITACC/consolidated/ |
| FIVESTAR | Five-Star Business Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=FIVESTAR | https://www.screener.in/company/FIVESTAR/consolidated/ |
| HDBFS | HDB Financial Services Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=HDBFS | https://www.screener.in/company/HDBFS/consolidated/ |
| IIFL | IIFL Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=IIFL | https://www.screener.in/company/IIFL/consolidated/ |
| JMFINANCIL | JM Financial Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=JMFINANCIL | https://www.screener.in/company/JMFINANCIL/consolidated/ |
| LTF | L&T Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=LTF | https://www.screener.in/company/LTF/consolidated/ |
| M&MFIN | Mahindra & Mahindra Financial Services Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=M%26MFIN | https://www.screener.in/company/M%26MFIN/consolidated/ |
| MANAPPURAM | Manappuram Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=MANAPPURAM | https://www.screener.in/company/MANAPPURAM/consolidated/ |
| MUTHOOTFIN | Muthoot Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=MUTHOOTFIN | https://www.screener.in/company/MUTHOOTFIN/consolidated/ |
| PIRAMALFIN | Piramal Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=PIRAMALFIN | https://www.screener.in/company/PIRAMALFIN/consolidated/ |
| POONAWALLA | Poonawalla Fincorp Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=POONAWALLA | https://www.screener.in/company/POONAWALLA/consolidated/ |
| SAMMAANCAP | Sammaan Capital Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=SAMMAANCAP | https://www.screener.in/company/SAMMAANCAP/consolidated/ |
| SBICARD | SBI Cards and Payment Services Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=SBICARD | https://www.screener.in/company/SBICARD/consolidated/ |
| SHRIRAMFIN | Shriram Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=SHRIRAMFIN | https://www.screener.in/company/SHRIRAMFIN/consolidated/ |
| SUNDARMFIN | Sundaram Finance Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=SUNDARMFIN | https://www.screener.in/company/SUNDARMFIN/consolidated/ |
| TATACAP | Tata Capital Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=TATACAP | https://www.screener.in/company/TATACAP/consolidated/ |

### broker_exchange_infra (11)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| ANGELONE | Angel One Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=ANGELONE | https://www.screener.in/company/ANGELONE/consolidated/ |
| BSE | BSE Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=BSE | https://www.screener.in/company/BSE/consolidated/ |
| CAMS | Computer Age Management Services Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CAMS | https://www.screener.in/company/CAMS/consolidated/ |
| CDSL | Central Depository Services (India) Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CDSL | https://www.screener.in/company/CDSL/consolidated/ |
| CHOICEIN | Choice International Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CHOICEIN | https://www.screener.in/company/CHOICEIN/consolidated/ |
| CRISIL | CRISIL Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CRISIL | https://www.screener.in/company/CRISIL/consolidated/ |
| GROWW | Billionbrains Garage Ventures Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=GROWW | https://www.screener.in/company/GROWW/consolidated/ |
| IEX | Indian Energy Exchange Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=IEX | https://www.screener.in/company/IEX/consolidated/ |
| KFINTECH | Kfin Technologies Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=KFINTECH | https://www.screener.in/company/KFINTECH/consolidated/ |
| MCX | Multi Commodity Exchange of India Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=MCX | https://www.screener.in/company/MCX/consolidated/ |
| MOTILALOFS | Motilal Oswal Financial Services Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=MOTILALOFS | https://www.screener.in/company/MOTILALOFS/consolidated/ |

### holding_investment (6)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| AIIL | Authum Investment & Infrastructure Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=AIIL | https://www.screener.in/company/AIIL/consolidated/ |
| BAJAJFINSV | Bajaj Finserv Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=BAJAJFINSV | https://www.screener.in/company/BAJAJFINSV/consolidated/ |
| BAJAJHLDNG | Bajaj Holdings & Investment Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=BAJAJHLDNG | https://www.screener.in/company/BAJAJHLDNG/consolidated/ |
| CHOLAHLDNG | Cholamandalam Financial Holdings Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CHOLAHLDNG | https://www.screener.in/company/CHOLAHLDNG/consolidated/ |
| JIOFIN | Jio Financial Services Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=JIOFIN | https://www.screener.in/company/JIOFIN/consolidated/ |
| TATAINVEST | Tata Investment Corporation Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=TATAINVEST | https://www.screener.in/company/TATAINVEST/consolidated/ |

### bank_private (13)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| AXISBANK | Axis Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=AXISBANK | https://www.screener.in/company/AXISBANK/consolidated/ |
| BANDHANBNK | Bandhan Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=BANDHANBNK | https://www.screener.in/company/BANDHANBNK/consolidated/ |
| CUB | City Union Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CUB | https://www.screener.in/company/CUB/consolidated/ |
| FEDERALBNK | Federal Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=FEDERALBNK | https://www.screener.in/company/FEDERALBNK/consolidated/ |
| HDFCBANK | HDFC Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=HDFCBANK | https://www.screener.in/company/HDFCBANK/consolidated/ |
| ICICIBANK | ICICI Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=ICICIBANK | https://www.screener.in/company/ICICIBANK/consolidated/ |
| IDFCFIRSTB | IDFC First Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=IDFCFIRSTB | https://www.screener.in/company/IDFCFIRSTB/consolidated/ |
| INDUSINDBK | IndusInd Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=INDUSINDBK | https://www.screener.in/company/INDUSINDBK/consolidated/ |
| J&KBANK | Jammu & Kashmir Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=J%26KBANK | https://www.screener.in/company/J%26KBANK/consolidated/ |
| KARURVYSYA | Karur Vysya Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=KARURVYSYA | https://www.screener.in/company/KARURVYSYA/consolidated/ |
| KOTAKBANK | Kotak Mahindra Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=KOTAKBANK | https://www.screener.in/company/KOTAKBANK/consolidated/ |
| RBLBANK | RBL Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=RBLBANK | https://www.screener.in/company/RBLBANK/consolidated/ |
| YESBANK | Yes Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=YESBANK | https://www.screener.in/company/YESBANK/consolidated/ |

### bank_psu (12)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| BANKBARODA | Bank of Baroda | https://www.nseindia.com/get-quotes/equity?symbol=BANKBARODA | https://www.screener.in/company/BANKBARODA/consolidated/ |
| BANKINDIA | Bank of India | https://www.nseindia.com/get-quotes/equity?symbol=BANKINDIA | https://www.screener.in/company/BANKINDIA/consolidated/ |
| CANBK | Canara Bank | https://www.nseindia.com/get-quotes/equity?symbol=CANBK | https://www.screener.in/company/CANBK/consolidated/ |
| CENTRALBK | Central Bank of India | https://www.nseindia.com/get-quotes/equity?symbol=CENTRALBK | https://www.screener.in/company/CENTRALBK/consolidated/ |
| IDBI | IDBI Bank Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=IDBI | https://www.screener.in/company/IDBI/consolidated/ |
| INDIANB | Indian Bank | https://www.nseindia.com/get-quotes/equity?symbol=INDIANB | https://www.screener.in/company/INDIANB/consolidated/ |
| IOB | Indian Overseas Bank | https://www.nseindia.com/get-quotes/equity?symbol=IOB | https://www.screener.in/company/IOB/consolidated/ |
| MAHABANK | Bank of Maharashtra | https://www.nseindia.com/get-quotes/equity?symbol=MAHABANK | https://www.screener.in/company/MAHABANK/consolidated/ |
| PNB | Punjab National Bank | https://www.nseindia.com/get-quotes/equity?symbol=PNB | https://www.screener.in/company/PNB/consolidated/ |
| SBIN | State Bank of India | https://www.nseindia.com/get-quotes/equity?symbol=SBIN | https://www.screener.in/company/SBIN/consolidated/ |
| UCOBANK | UCO Bank | https://www.nseindia.com/get-quotes/equity?symbol=UCOBANK | https://www.screener.in/company/UCOBANK/consolidated/ |
| UNIONBANK | Union Bank of India | https://www.nseindia.com/get-quotes/equity?symbol=UNIONBANK | https://www.screener.in/company/UNIONBANK/consolidated/ |

### insurance_life (6)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| CANHLIFE | Canara HSBC Life Insurance Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=CANHLIFE | https://www.screener.in/company/CANHLIFE/consolidated/ |
| HDFCLIFE | HDFC Life Insurance Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=HDFCLIFE | https://www.screener.in/company/HDFCLIFE/consolidated/ |
| ICICIPRULI | ICICI Prudential Life Insurance Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=ICICIPRULI | https://www.screener.in/company/ICICIPRULI/consolidated/ |
| LICI | Life Insurance Corporation of India | https://www.nseindia.com/get-quotes/equity?symbol=LICI | https://www.screener.in/company/LICI/consolidated/ |
| MFSL | Max Financial Services Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=MFSL | https://www.screener.in/company/MFSL/consolidated/ |
| SBILIFE | SBI Life Insurance Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=SBILIFE | https://www.screener.in/company/SBILIFE/consolidated/ |

### insurance_general (4)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| GICRE | General Insurance Corporation of India | https://www.nseindia.com/get-quotes/equity?symbol=GICRE | https://www.screener.in/company/GICRE/consolidated/ |
| ICICIGI | ICICI Lombard General Insurance Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=ICICIGI | https://www.screener.in/company/ICICIGI/consolidated/ |
| NIACL | The New India Assurance Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=NIACL | https://www.screener.in/company/NIACL/consolidated/ |
| STARHEALTH | Star Health and Allied Insurance Company Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=STARHEALTH | https://www.screener.in/company/STARHEALTH/consolidated/ |

### govt_financier (6)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| HUDCO | Housing & Urban Development Corporation Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=HUDCO | https://www.screener.in/company/HUDCO/consolidated/ |
| IFCI | IFCI Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=IFCI | https://www.screener.in/company/IFCI/consolidated/ |
| IREDA | Indian Renewable Energy Development Agency Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=IREDA | https://www.screener.in/company/IREDA/consolidated/ |
| IRFC | Indian Railway Finance Corporation Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=IRFC | https://www.screener.in/company/IRFC/consolidated/ |
| PFC | Power Finance Corporation Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=PFC | https://www.screener.in/company/PFC/consolidated/ |
| RECLTD | REC Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=RECLTD | https://www.screener.in/company/RECLTD/consolidated/ |

### fintech_payments (3)

| Symbol | Company | NSE page | Screener (read manually; check its terms before automating) |
|---|---|---|---|
| PAYTM | One 97 Communications Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=PAYTM | https://www.screener.in/company/PAYTM/consolidated/ |
| PINELABS | Pine Labs Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=PINELABS | https://www.screener.in/company/PINELABS/consolidated/ |
| POLICYBZR | PB Fintech Ltd. | https://www.nseindia.com/get-quotes/equity?symbol=POLICYBZR | https://www.screener.in/company/POLICYBZR/consolidated/ |
