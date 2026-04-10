# Harris County Motivated Seller Lead Scraper

Automated daily scraper for motivated seller leads from the **Harris County Clerk's Official Public Records** portal, enriched with parcel/address data from the **Harris County Appraisal District (HCAD)** bulk data download.

---

## Features

| Feature | Detail |
|---|---|
| **Data Source** | Harris County Clerk — Official Public Records |
| **Lookback Window** | 7 days (configurable via `LOOKBACK_DAYS` env var) |
| **Document Types** | LP, NOFC, TAXDEED, JUD/CCJ/DRJUD, LNCORPTX/LNIRS/LNFED, LN/LNMECH/LNHOA, MEDLN, PRO, NOC, RELLP |
| **Address Enrichment** | HCAD bulk DBF parcel download (property + mailing address) |
| **Seller Scoring** | 0–100 composite score |
| **GHL Export** | One-click CSV export for Go High Level import |
| **Dashboard** | GitHub Pages — filterable, sortable, searchable table |
| **Schedule** | Daily at 07:00 UTC via GitHub Actions |

---

## Quick Start

### 1. Clone & configure

```bash
git clone https://github.com/YOUR_ORG/YOUR_REPO.git
cd YOUR_REPO
```

### 2. Enable GitHub Pages

In **Settings → Pages**, set source to **GitHub Actions**.

### 3. Run manually

Trigger the workflow via **Actions → Scrape Harris County Motivated Seller Leads → Run workflow**.

Or run locally:

```bash
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium
python scraper/fetch.py
```

---

## File Structure

```
.
├── .github/
│   └── workflows/
│       └── scrape.yml          # Daily GitHub Actions workflow
├── scraper/
│   ├── fetch.py                # Main scraper (Playwright + requests)
│   └── requirements.txt
├── dashboard/
│   ├── index.html              # Lead dashboard (GitHub Pages)
│   └── records.json            # Latest scraped records
└── data/
    ├── records.json            # Duplicate of dashboard/records.json
    └── ghl_export.csv          # GHL-ready CSV export
```

---

## Seller Score Formula

| Condition | Points |
|---|---|
| Base score | 30 |
| Per flag (Lis pendens, Pre-foreclosure, Judgment, Tax lien, etc.) | +10 each |
| LP + Foreclosure combo | +20 bonus |
| Amount > $100k | +15 |
| Amount > $50k | +10 |
| Filed within lookback window | +5 |
| Has property or mailing address | +5 |
| **Maximum** | **100** |

---

## GHL Export Columns

`First Name`, `Last Name`, `Mailing Address`, `Mailing City`, `Mailing State`, `Mailing Zip`, `Property Address`, `Property City`, `Property State`, `Property Zip`, `Lead Type`, `Document Type`, `Date Filed`, `Document Number`, `Amount/Debt Owed`, `Seller Score`, `Motivated Seller Flags`, `Source`, `Public Records URL`

---

## Configuration

| Env Var | Default | Description |
|---|---|---|
| `LOOKBACK_DAYS` | `7` | Days to look back for new documents |

---

## Notes on the Clerk Portal

The Harris County Clerk portal (`cclerk.hctx.net`) uses ASP.NET WebForms. The scraper uses:

1. **Playwright (Chromium)** as the primary method — handles JavaScript rendering and `__doPostBack` form submissions.
2. **requests + BeautifulSoup** as a fallback — attempts direct HTTP POST with ViewState tokens.

If the portal changes its form structure, update the selector logic in `_scrape_doc_type()` and `_http_search()` in `scraper/fetch.py`.

---

## HCAD Parcel Data

The scraper attempts to download parcel data from multiple HCAD bulk data URLs. The data is used to:
- Match owner names to property records (3 name-format variants)
- Populate `prop_address`, `prop_city`, `prop_zip`
- Populate `mail_address`, `mail_city`, `mail_state`, `mail_zip`

If HCAD data is unavailable, records are still saved — just without address enrichment.

