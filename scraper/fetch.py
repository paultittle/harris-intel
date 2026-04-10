"""
Harris County Motivated Seller Lead Scraper
Fetches LP, NOFC, TAXDEED, JUD, LIEN, PROBATE, NOC records from the
Harris County Clerk's Official Public Records portal and enriches them
with parcel / mailing-address data from the HCAD bulk DBF download.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Optional Playwright import – graceful fallback so the module still loads
# during unit tests / import-only usage.
# ---------------------------------------------------------------------------
try:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    logging.warning("Playwright not installed – clerk portal scraping disabled.")

try:
    from dbfread import DBF
    HAS_DBF = True
except ImportError:
    HAS_DBF = False
    logging.warning("dbfread not installed – parcel enrichment disabled.")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOOKBACK_DAYS: int = int(os.getenv("LOOKBACK_DAYS", "7"))

CLERK_BASE = "https://www.cclerk.hctx.net/Applications/WebSearch/PR.aspx"
CLERK_SEARCH = "https://www.cclerk.hctx.net/Applications/WebSearch/PR.aspx"

# HCAD bulk parcel data (Real Property account export, updated nightly)
HCAD_BULK_BASE = "https://pdata.hcad.org"
HCAD_PARCEL_ZIP = "https://pdata.hcad.org/data/cama/2024/account_appraiser.zip"
# Fallback direct DBF URLs tried in order
HCAD_DBF_URLS = [
    "https://pdata.hcad.org/data/cama/2024/account_appraiser.zip",
    "https://pdata.hcad.org/Pdata/pdata.zip",
    "https://pdata.hcad.org/data/2024/account_appraiser.zip",
]

# Document type → category mapping
DOC_TYPE_MAP: dict[str, tuple[str, str]] = {
    # (category_code, human_label)
    "LP":       ("LP",      "Lis Pendens"),
    "NOFC":     ("NOFC",    "Notice of Foreclosure"),
    "TAXDEED":  ("TAXDEED", "Tax Deed"),
    "JUD":      ("JUD",     "Judgment"),
    "CCJ":      ("JUD",     "Certified Judgment"),
    "DRJUD":    ("JUD",     "Domestic Judgment"),
    "LNCORPTX": ("LIEN",    "Corp Tax Lien"),
    "LNIRS":    ("LIEN",    "IRS Lien"),
    "LNFED":    ("LIEN",    "Federal Lien"),
    "LN":       ("LIEN",    "Lien"),
    "LNMECH":   ("LIEN",    "Mechanic Lien"),
    "LNHOA":    ("LIEN",    "HOA Lien"),
    "MEDLN":    ("LIEN",    "Medicaid Lien"),
    "PRO":      ("PRO",     "Probate Document"),
    "NOC":      ("NOC",     "Notice of Commencement"),
    "RELLP":    ("RELLP",   "Release Lis Pendens"),
}

# Search codes used in the clerk portal dropdown
SEARCH_DOC_TYPES: list[str] = list(DOC_TYPE_MAP.keys())

OUTPUT_PATHS = [
    Path("dashboard/records.json"),
    Path("data/records.json"),
]

GHL_CSV_PATH = Path("data/ghl_export.csv")

GHL_COLUMNS = [
    "First Name", "Last Name", "Mailing Address", "Mailing City",
    "Mailing State", "Mailing Zip", "Property Address", "Property City",
    "Property State", "Property Zip", "Lead Type", "Document Type",
    "Date Filed", "Document Number", "Amount/Debt Owed",
    "Seller Score", "Motivated Seller Flags", "Source", "Public Records URL",
]

# ---------------------------------------------------------------------------
# Parcel lookup helpers
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().upper())


def _name_variants(full_name: str) -> list[str]:
    """Return possible name variants for fuzzy owner matching."""
    n = _normalize(full_name)
    parts = n.split()
    variants = [n]
    if len(parts) >= 2:
        # LAST FIRST  (swap first two tokens)
        variants.append(f"{parts[-1]} {' '.join(parts[:-1])}")
        # LAST, FIRST
        variants.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
    return list(dict.fromkeys(variants))


class ParcelDB:
    """Loads the HCAD bulk parcel DBF and provides fast owner-name lookup."""

    def __init__(self) -> None:
        self._by_owner: dict[str, list[dict]] = {}
        self._by_acct: dict[str, dict] = {}
        self.loaded = False

    # ------------------------------------------------------------------
    def load_from_file(self, path: Path) -> None:
        if not HAS_DBF:
            log.warning("dbfread unavailable – skipping parcel load.")
            return
        log.info("Loading parcel DBF from %s …", path)
        try:
            table = DBF(str(path), encoding="latin-1", ignore_missing_memofile=True)
            for rec in table:
                self._ingest(dict(rec))
            self.loaded = True
            log.info("Parcel DB loaded: %d accounts, %d owner keys",
                     len(self._by_acct), len(self._by_owner))
        except Exception as exc:
            log.error("Failed to load DBF: %s", exc)

    def load_from_zip(self, zip_bytes: bytes) -> None:
        if not HAS_DBF:
            return
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
                if not dbf_names:
                    log.error("No DBF found inside ZIP.")
                    return
                dbf_name = dbf_names[0]
                log.info("Extracting %s from ZIP …", dbf_name)
                tmp = Path("/tmp/parcel_extract.dbf")
                tmp.write_bytes(zf.read(dbf_name))
                self.load_from_file(tmp)
        except Exception as exc:
            log.error("ZIP extraction error: %s", exc)

    def _ingest(self, rec: dict) -> None:
        def g(*keys: str) -> str:
            for k in keys:
                v = rec.get(k) or rec.get(k.upper()) or rec.get(k.lower())
                if v:
                    return str(v).strip()
            return ""

        acct   = g("ACCOUNT", "ACCT", "PARCEL_ID")
        owner  = g("OWNER", "OWN1", "OWNERNAME", "OWNER1")
        saddr  = g("SITE_ADDR", "SITEADDR", "SITE_ADDRESS", "PROP_ADDR")
        scity  = g("SITE_CITY", "SITECITY", "PROP_CITY")
        szip   = g("SITE_ZIP",  "SITEZIP",  "PROP_ZIP")
        maddr  = g("ADDR_1", "MAILADR1", "MAIL_ADDR", "MAIL1")
        mcity  = g("CITY", "MAILCITY", "MAIL_CITY")
        mstate = g("STATE", "MAILSTATE", "MAIL_STATE")
        mzip   = g("ZIP", "MAILZIP", "MAIL_ZIP")

        entry = {
            "acct": acct,
            "owner": owner,
            "prop_address": saddr,
            "prop_city": scity,
            "prop_state": "TX",
            "prop_zip": szip,
            "mail_address": maddr,
            "mail_city": mcity,
            "mail_state": mstate or "TX",
            "mail_zip": mzip,
        }
        if acct:
            self._by_acct[acct] = entry
        if owner:
            key = _normalize(owner)
            self._by_owner.setdefault(key, []).append(entry)
            for v in _name_variants(owner):
                self._by_owner.setdefault(v, []).append(entry)

    # ------------------------------------------------------------------
    def lookup(self, owner_name: str) -> dict | None:
        if not owner_name:
            return None
        for variant in _name_variants(owner_name):
            hits = self._by_owner.get(variant)
            if hits:
                return hits[0]
        return None

    def lookup_by_acct(self, acct: str) -> dict | None:
        return self._by_acct.get(acct.strip())


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def compute_flags(record: dict) -> list[str]:
    flags: list[str] = []
    cat = record.get("cat", "")
    doc_type = record.get("doc_type", "").upper()
    owner = record.get("owner", "")
    amount = record.get("amount") or 0
    filed_str = record.get("filed", "")

    if cat == "LP":
        flags.append("Lis pendens")
    if cat == "NOFC":
        flags.append("Pre-foreclosure")
    if cat in ("JUD",):
        flags.append("Judgment lien")
    if doc_type in ("LNCORPTX", "LNIRS", "LNFED", "TAXDEED"):
        flags.append("Tax lien")
    if doc_type in ("LNMECH",):
        flags.append("Mechanic lien")
    if cat == "PRO":
        flags.append("Probate / estate")
    if re.search(r"\b(LLC|CORP|INC|LTD|LP|GP|TRUST|ASSOC)\b", owner.upper()):
        flags.append("LLC / corp owner")

    # "New this week" – filed within LOOKBACK_DAYS
    try:
        filed_dt = datetime.strptime(filed_str[:10], "%Y-%m-%d")
        cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)
        if filed_dt >= cutoff:
            flags.append("New this week")
    except Exception:
        pass

    return list(dict.fromkeys(flags))


def compute_score(record: dict, flags: list[str]) -> int:
    score = 30  # base

    per_flag = {
        "Lis pendens": 10,
        "Pre-foreclosure": 10,
        "Judgment lien": 10,
        "Tax lien": 10,
        "Mechanic lien": 10,
        "Probate / estate": 10,
        "LLC / corp owner": 10,
        "New this week": 5,
    }
    for f in flags:
        score += per_flag.get(f, 0)

    # LP + foreclosure combo
    if "Lis pendens" in flags and "Pre-foreclosure" in flags:
        score += 20

    amount = record.get("amount") or 0
    if amount > 100_000:
        score += 15
    elif amount > 50_000:
        score += 10

    if record.get("prop_address") or record.get("mail_address"):
        score += 5

    return min(score, 100)


# ---------------------------------------------------------------------------
# HCAD bulk data downloader
# ---------------------------------------------------------------------------

def download_hcad_parcel_db(session: requests.Session) -> ParcelDB:
    db = ParcelDB()
    for url in HCAD_DBF_URLS:
        log.info("Trying HCAD parcel data from %s …", url)
        for attempt in range(1, 4):
            try:
                r = session.get(url, timeout=120, stream=True)
                if r.status_code == 200:
                    content = r.content
                    if url.endswith(".zip"):
                        db.load_from_zip(content)
                    else:
                        tmp = Path("/tmp/parcel.dbf")
                        tmp.write_bytes(content)
                        db.load_from_file(tmp)
                    if db.loaded:
                        return db
                else:
                    log.warning("HTTP %d for %s", r.status_code, url)
            except Exception as exc:
                log.warning("Attempt %d failed for %s: %s", attempt, url, exc)
                time.sleep(2 ** attempt)
    log.warning("Could not load HCAD parcel data – addresses will be empty.")
    return db


# ---------------------------------------------------------------------------
# Clerk portal scraper (Playwright async)
# ---------------------------------------------------------------------------

def _parse_amount(text: str) -> float:
    """Extract numeric dollar amount from a string."""
    clean = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(clean)
    except ValueError:
        return 0.0


def _clerk_doc_url(doc_num: str) -> str:
    """Build a direct URL to a Harris County Clerk document."""
    # Standard pattern for the Harris County Official Public Records viewer
    return (
        f"https://www.cclerk.hctx.net/Applications/WebSearch/PR.aspx"
        f"?DocNum={doc_num}"
    )


async def _scrape_doc_type(
    page,
    doc_type_code: str,
    start_date: str,
    end_date: str,
    base_url: str,
) -> list[dict]:
    """Scrape one document type from the Harris County Clerk portal."""
    records: list[dict] = []
    cat, cat_label = DOC_TYPE_MAP.get(doc_type_code, ("OTHER", doc_type_code))

    log.info("Scraping %s (%s) %s → %s …", doc_type_code, cat_label,
             start_date, end_date)

    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception as exc:
        log.warning("Page load error for %s: %s", doc_type_code, exc)
        return records

    # ---- Fill search form ----
    try:
        # Document type dropdown
        await page.select_option("select[name*='DocType'], select[id*='DocType']",
                                  value=doc_type_code, timeout=5_000)
    except Exception:
        try:
            await page.fill("input[name*='DocType'], input[id*='DocType']",
                             doc_type_code, timeout=3_000)
        except Exception:
            log.debug("Could not set doc type for %s", doc_type_code)

    try:
        # Start date
        for sel in ["input[name*='StartDate']", "input[id*='StartDate']",
                    "input[name*='FromDate']", "input[id*='FromDate']"]:
            try:
                await page.fill(sel, start_date, timeout=3_000)
                break
            except Exception:
                pass
        # End date
        for sel in ["input[name*='EndDate']", "input[id*='EndDate']",
                    "input[name*='ToDate']", "input[id*='ToDate']"]:
            try:
                await page.fill(sel, end_date, timeout=3_000)
                break
            except Exception:
                pass
    except Exception as exc:
        log.warning("Date fill error for %s: %s", doc_type_code, exc)

    # Submit search
    try:
        await page.click(
            "input[type='submit'][value*='Search'], "
            "button[type='submit'], "
            "input[id*='Search'], "
            "input[name*='Search']",
            timeout=5_000,
        )
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception as exc:
        log.warning("Search submit error for %s: %s", doc_type_code, exc)
        return records

    # ---- Parse results pages ----
    page_num = 0
    while True:
        page_num += 1
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        # Find results table – look for a table with document rows
        table = (
            soup.find("table", {"id": re.compile(r"(result|grid|data)", re.I)})
            or soup.find("table", class_=re.compile(r"(result|grid|data)", re.I))
            or _find_data_table(soup)
        )

        if table is None:
            log.debug("No table found on page %d for %s", page_num, doc_type_code)
            break

        rows = table.find_all("tr")
        header_row = rows[0] if rows else None
        if header_row is None:
            break

        headers = [th.get_text(strip=True).upper() for th in
                   header_row.find_all(["th", "td"])]

        data_rows = rows[1:]
        if not data_rows:
            break

        for tr in data_rows:
            cells = tr.find_all("td")
            if not cells:
                continue
            try:
                row_data = _extract_row(cells, headers, doc_type_code,
                                         cat, cat_label)
                if row_data:
                    records.append(row_data)
            except Exception as exc:
                log.debug("Row parse error: %s", exc)

        # Check for next page
        next_btn = soup.find(
            "a", string=re.compile(r"next|>", re.I)
        ) or soup.find("input", {"value": re.compile(r"next|>", re.I)})

        if next_btn and page_num < 50:
            try:
                await page.click(
                    "a:has-text('Next'), input[value*='Next'], "
                    "a[id*='Next'], input[id*='Next']",
                    timeout=5_000,
                )
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                break
        else:
            break

    log.info("  → %d records for %s", len(records), doc_type_code)
    return records


def _find_data_table(soup: BeautifulSoup):
    """Heuristic: find the table with the most data rows."""
    tables = soup.find_all("table")
    best = None
    best_rows = 0
    for t in tables:
        rows = len(t.find_all("tr"))
        if rows > best_rows:
            best_rows = rows
            best = t
    return best if best_rows > 2 else None


def _col(cells: list, headers: list[str], *names: str, default: str = "") -> str:
    for name in names:
        for i, h in enumerate(headers):
            if name.upper() in h:
                if i < len(cells):
                    return cells[i].get_text(strip=True)
    # Fall back to positional
    idx_map = {"DOC": 0, "TYPE": 1, "DATE": 2,
               "GRANTOR": 3, "GRANTEE": 4, "LEGAL": 5, "AMOUNT": 6}
    for name in names:
        pos = idx_map.get(name.upper())
        if pos is not None and pos < len(cells):
            return cells[pos].get_text(strip=True)
    return default


def _extract_row(
    cells,
    headers: list[str],
    doc_type_code: str,
    cat: str,
    cat_label: str,
) -> dict | None:
    doc_num  = _col(cells, headers, "DOCNUM", "DOC_NUM", "DOCUMENT", "DOC")
    filed    = _col(cells, headers, "FILED", "DATE", "FILEDDATE", "REC")
    grantor  = _col(cells, headers, "GRANTOR", "OWNER", "FROM")
    grantee  = _col(cells, headers, "GRANTEE", "TO", "LENDER")
    legal    = _col(cells, headers, "LEGAL", "DESCRIPTION", "ABSTRACT")
    amount_s = _col(cells, headers, "AMOUNT", "AMT", "VALUE")
    dtype    = _col(cells, headers, "TYPE", "DOCTYPE") or doc_type_code

    if not doc_num and not grantor:
        return None

    # Parse filed date to ISO
    filed_iso = _parse_date(filed)

    # Build document URL
    clerk_url = ""
    # Look for an anchor with a doc number or view link
    for td in cells:
        a = td.find("a", href=True)
        if a:
            href = a["href"]
            if "DocNum" in href or "docnum" in href or "view" in href.lower():
                if href.startswith("http"):
                    clerk_url = href
                else:
                    clerk_url = "https://www.cclerk.hctx.net" + href
            break
    if not clerk_url and doc_num:
        clerk_url = _clerk_doc_url(doc_num)

    amount = _parse_amount(amount_s) if amount_s else 0.0

    return {
        "doc_num": doc_num,
        "doc_type": dtype or doc_type_code,
        "filed": filed_iso,
        "cat": cat,
        "cat_label": cat_label,
        "owner": grantor,
        "grantee": grantee,
        "amount": amount,
        "legal": legal,
        "prop_address": "",
        "prop_city": "",
        "prop_state": "TX",
        "prop_zip": "",
        "mail_address": "",
        "mail_city": "",
        "mail_state": "",
        "mail_zip": "",
        "clerk_url": clerk_url,
        "flags": [],
        "score": 0,
    }


def _parse_date(text: str) -> str:
    """Attempt to parse a date string into YYYY-MM-DD."""
    if not text:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y",
                "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip()[:20], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return text[:10]


# ---------------------------------------------------------------------------
# Alternative HTTP-based scraper (fallback / supplement)
# ---------------------------------------------------------------------------

def _http_search(
    session: requests.Session,
    doc_type_code: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """
    Attempt a direct HTTP POST to the clerk search endpoint.
    Harris County uses ASP.NET WebForms (__VIEWSTATE / __doPostBack).
    """
    cat, cat_label = DOC_TYPE_MAP.get(doc_type_code, ("OTHER", doc_type_code))
    records: list[dict] = []

    # First GET to obtain viewstate
    try:
        r = session.get(CLERK_BASE, timeout=20)
        r.raise_for_status()
    except Exception as exc:
        log.warning("HTTP GET clerk base failed: %s", exc)
        return records

    soup = BeautifulSoup(r.text, "lxml")

    def vs(name: str) -> str:
        el = soup.find("input", {"name": name})
        return el["value"] if el and el.get("value") else ""

    payload = {
        "__VIEWSTATE": vs("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": vs("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": vs("__EVENTVALIDATION"),
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        # Common field names for Harris County clerk search
        "ctl00$ContentPlaceHolder1$DocType": doc_type_code,
        "ctl00$ContentPlaceHolder1$StartDate": start_date,
        "ctl00$ContentPlaceHolder1$EndDate": end_date,
        "ctl00$ContentPlaceHolder1$btnSearch": "Search",
    }

    for attempt in range(1, 4):
        try:
            r2 = session.post(CLERK_BASE, data=payload, timeout=30)
            r2.raise_for_status()
            break
        except Exception as exc:
            log.warning("HTTP POST attempt %d failed for %s: %s",
                        attempt, doc_type_code, exc)
            time.sleep(2 ** attempt)
    else:
        return records

    soup2 = BeautifulSoup(r2.text, "lxml")
    table = _find_data_table(soup2)
    if not table:
        return records

    rows = table.find_all("tr")
    if len(rows) < 2:
        return records

    headers = [th.get_text(strip=True).upper()
               for th in rows[0].find_all(["th", "td"])]

    for tr in rows[1:]:
        cells = tr.find_all("td")
        if not cells:
            continue
        try:
            row = _extract_row(cells, headers, doc_type_code, cat, cat_label)
            if row:
                records.append(row)
        except Exception as exc:
            log.debug("HTTP row parse error: %s", exc)

    return records


# ---------------------------------------------------------------------------
# Main scrape orchestration
# ---------------------------------------------------------------------------

async def scrape_clerk_async(
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Use Playwright to scrape all document types from the clerk portal."""
    all_records: list[dict] = []

    if not HAS_PLAYWRIGHT:
        log.warning("Playwright unavailable – using HTTP fallback only.")
        return all_records

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        for doc_type in SEARCH_DOC_TYPES:
            for attempt in range(1, 4):
                try:
                    recs = await _scrape_doc_type(
                        page, doc_type, start_date, end_date, CLERK_SEARCH
                    )
                    all_records.extend(recs)
                    break
                except Exception as exc:
                    log.warning("Playwright attempt %d/%d for %s: %s",
                                attempt, 3, doc_type, exc)
                    await asyncio.sleep(2 ** attempt)

        await browser.close()

    return all_records


def scrape_clerk_http(
    session: requests.Session,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """HTTP fallback scraper for all document types."""
    all_records: list[dict] = []
    for doc_type in SEARCH_DOC_TYPES:
        for attempt in range(1, 4):
            try:
                recs = _http_search(session, doc_type, start_date, end_date)
                all_records.extend(recs)
                break
            except Exception as exc:
                log.warning("HTTP attempt %d for %s: %s", attempt, doc_type, exc)
                time.sleep(2 ** attempt)
    return all_records


# ---------------------------------------------------------------------------
# Enrichment: attach parcel addresses to records
# ---------------------------------------------------------------------------

def enrich_records(records: list[dict], parcel_db: ParcelDB) -> list[dict]:
    enriched = 0
    for rec in records:
        owner = rec.get("owner", "")
        hit = parcel_db.lookup(owner)
        if hit:
            rec["prop_address"] = hit["prop_address"]
            rec["prop_city"]    = hit["prop_city"]
            rec["prop_state"]   = hit["prop_state"]
            rec["prop_zip"]     = hit["prop_zip"]
            rec["mail_address"] = hit["mail_address"]
            rec["mail_city"]    = hit["mail_city"]
            rec["mail_state"]   = hit["mail_state"]
            rec["mail_zip"]     = hit["mail_zip"]
            enriched += 1

    log.info("Enriched %d / %d records with parcel data.", enriched, len(records))
    return records


def apply_scores(records: list[dict]) -> list[dict]:
    for rec in records:
        flags = compute_flags(rec)
        score = compute_score(rec, flags)
        rec["flags"] = flags
        rec["score"] = score
    return records


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(records: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for rec in records:
        key = rec.get("doc_num") or f"{rec.get('owner')}|{rec.get('filed')}"
        if key and key not in seen:
            seen.add(key)
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], " ".join(parts[1:])


def save_records(records: list[dict], start_date: str, end_date: str) -> None:
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "Harris County Clerk – Official Public Records",
        "date_range": {"from": start_date, "to": end_date},
        "total": len(records),
        "with_address": sum(
            1 for r in records if r.get("prop_address") or r.get("mail_address")
        ),
        "records": records,
    }
    for path in OUTPUT_PATHS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        log.info("Saved %d records → %s", len(records), path)


def save_ghl_csv(records: list[dict]) -> None:
    GHL_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GHL_CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=GHL_COLUMNS)
        writer.writeheader()
        for rec in records:
            first, last = _split_name(rec.get("owner", ""))
            writer.writerow({
                "First Name": first,
                "Last Name": last,
                "Mailing Address": rec.get("mail_address", ""),
                "Mailing City": rec.get("mail_city", ""),
                "Mailing State": rec.get("mail_state", ""),
                "Mailing Zip": rec.get("mail_zip", ""),
                "Property Address": rec.get("prop_address", ""),
                "Property City": rec.get("prop_city", ""),
                "Property State": rec.get("prop_state", "TX"),
                "Property Zip": rec.get("prop_zip", ""),
                "Lead Type": rec.get("cat_label", ""),
                "Document Type": rec.get("doc_type", ""),
                "Date Filed": rec.get("filed", ""),
                "Document Number": rec.get("doc_num", ""),
                "Amount/Debt Owed": rec.get("amount", ""),
                "Seller Score": rec.get("score", 0),
                "Motivated Seller Flags": "; ".join(rec.get("flags", [])),
                "Source": "Harris County Clerk",
                "Public Records URL": rec.get("clerk_url", ""),
            })
    log.info("GHL CSV saved → %s", GHL_CSV_PATH)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)

    # Harris County Clerk portal uses MM/DD/YYYY format
    start_date = start_dt.strftime("%m/%d/%Y")
    end_date   = end_dt.strftime("%m/%d/%Y")

    log.info("=" * 60)
    log.info("Harris County Motivated Seller Lead Scraper")
    log.info("Date range: %s → %s  (lookback=%d days)",
             start_date, end_date, LOOKBACK_DAYS)
    log.info("=" * 60)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    })

    # 1. Download parcel data
    parcel_db = download_hcad_parcel_db(session)

    # 2. Scrape clerk records via Playwright
    records: list[dict] = []

    if HAS_PLAYWRIGHT:
        log.info("Starting Playwright-based clerk scrape …")
        try:
            records = await scrape_clerk_async(start_date, end_date)
        except Exception as exc:
            log.error("Playwright scrape failed: %s", exc)

    # 3. Supplement / fallback with HTTP scraper
    if not records:
        log.info("Falling back to HTTP-based clerk scrape …")
        records = scrape_clerk_http(session, start_date, end_date)

    log.info("Raw records collected: %d", len(records))

    # 4. Deduplicate
    records = deduplicate(records)
    log.info("After deduplication: %d", len(records))

    # 5. Enrich with parcel addresses
    records = enrich_records(records, parcel_db)

    # 6. Score
    records = apply_scores(records)

    # 7. Sort by score desc
    records.sort(key=lambda r: r.get("score", 0), reverse=True)

    # 8. Save outputs
    # Convert dates back to ISO for storage
    iso_start = start_dt.strftime("%Y-%m-%d")
    iso_end   = end_dt.strftime("%Y-%m-%d")

    save_records(records, iso_start, iso_end)
    save_ghl_csv(records)

    log.info("=" * 60)
    log.info("Done. %d motivated seller leads processed.", len(records))
    with_addr = sum(1 for r in records if r.get("prop_address") or r.get("mail_address"))
    log.info("  With address data: %d", with_addr)
    log.info("  Top score: %d", records[0]["score"] if records else 0)
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
