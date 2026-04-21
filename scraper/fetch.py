"""
Harris County Motivated Seller Lead Scraper
Fetches LP, NOFC, TAXDEED, JUD, LIEN, PROBATE, NOC records from the
Harris County Clerk's Real Property search portal and enriches them
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

import requests
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    logging.warning("Playwright not installed.")

try:
    from dbfread import DBF
    HAS_DBF = True
except ImportError:
    HAS_DBF = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

LOOKBACK_DAYS  = int(os.getenv("LOOKBACK_DAYS", "7"))
CLERK_RP_URL   = "https://www.cclerk.hctx.net/Applications/WebSearch/RP.aspx"
CLERK_FRCL_URL = "https://www.cclerk.hctx.net/Applications/WebSearch/FRCL_R.aspx"

# HCAD bulk data — tab-delimited text files
# Primary confirmed working URL (2025 dataset)
HCAD_TXT_URLS = [
    "https://pdata.hcad.org/data/2025/real_acct_owner.zip",
    "https://pdata.hcad.org/data/2026/real_acct_owner.zip",
    "https://pdata.hcad.org/data/2024/real_acct_owner.zip",
    "https://pdata.hcad.org/Pdata/real_acct_owner.zip",
]
# Fallback DBF URLs (older format)
HCAD_DBF_URLS = [
    "https://pdata.hcad.org/data/cama/2024/account_appraiser.zip",
    "https://pdata.hcad.org/Pdata/pdata.zip",
]

DOC_TYPE_MAP = {
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

OUTPUT_PATHS = [Path("dashboard/records.json"), Path("data/records.json")]
GHL_CSV_PATH = Path("data/ghl_export.csv")
GHL_COLUMNS  = [
    "First Name","Last Name","Mailing Address","Mailing City","Mailing State","Mailing Zip",
    "Property Address","Property City","Property State","Property Zip",
    "Lead Type","Document Type","Date Filed","Document Number","Amount/Debt Owed",
    "Seller Score","Motivated Seller Flags","Source","Public Records URL",
]


# ── Parcel DB ────────────────────────────────────────────────────────────────

def _norm(s):
    return re.sub(r"\s+", " ", str(s).strip().upper())

def _variants(name):
    n = _norm(name); p = n.split()
    v = [n]
    if len(p) >= 2:
        v += [f"{p[-1]} {' '.join(p[:-1])}", f"{p[-1]}, {' '.join(p[:-1])}"]
    return list(dict.fromkeys(v))

class ParcelDB:
    def __init__(self):
        self._idx = {}
        self.loaded = False

    def load_zip(self, data):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                log.info("ZIP contains: %s", names[:10])
                # Try txt/csv first (HCAD current format)
                txt_files = [n for n in names if n.lower().endswith((".txt",".csv"))]
                dbf_files = [n for n in names if n.lower().endswith(".dbf")]
                if txt_files:
                    raw = z.read(txt_files[0])
                    self._load_txt(raw)
                elif dbf_files and HAS_DBF:
                    tmp = Path("/tmp/p.dbf")
                    tmp.write_bytes(z.read(dbf_files[0]))
                    tbl = DBF(str(tmp), encoding="latin-1", ignore_missing_memofile=True)
                    for rec in tbl:
                        self._ingest(dict(rec))
                    self.loaded = True
                    log.info("Parcel DB (DBF): %d owner keys", len(self._idx))
        except Exception as e:
            log.error("Parcel load error: %s", e)

    def _load_txt(self, raw_bytes):
        """Load HCAD tab-delimited real_acct_owner.txt"""
        try:
            text = raw_bytes.decode("latin-1")
            lines = text.splitlines()
            if not lines:
                return
            # HCAD real_acct_owner columns (tab-delimited, no header):
            # 0:acct 1:name 2:addr_1 3:addr_2 4:addr_3
            # 5:city 6:state 7:zip 8:country
            # 9:site_addr 10:site_city 11:site_state 12:site_zip
            # Detect if first line is a header
            first = lines[0].split("\t")
            has_header = any(c.upper() in ("ACCT","ACCOUNT","NAME","OWNER") for c in first)
            if has_header:
                header = [c.strip().upper() for c in first]
                data_lines = lines[1:]
            else:
                header = None
                data_lines = lines

            count = 0
            for line in data_lines:
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                try:
                    if header:
                        def hg(*keys):
                            for k in keys:
                                if k in header:
                                    v = parts[header.index(k)].strip()
                                    if v: return v
                            return ""
                        owner      = hg("NAME","OWNER","OWN1","OWNERNAME")
                        mail_addr  = hg("ADDR_1","ADDRESS","MAILADR1","MAIL_ADDR","ADDR1")
                        mail_city  = hg("CITY","MAILCITY","MAIL_CITY")
                        mail_state = hg("STATE","MAILSTATE") or "TX"
                        mail_zip   = hg("ZIP","MAILZIP","MAIL_ZIP")
                        site_addr  = hg("SITE_ADDR","SITEADDR","SITE_ADDRESS","ADDR_SITE")
                        site_city  = hg("SITE_CITY","SITECITY")
                        site_zip   = hg("SITE_ZIP","SITEZIP")
                    else:
                        def p(i): return parts[i].strip() if i < len(parts) else ""
                        owner      = p(1)
                        mail_addr  = p(2)
                        mail_city  = p(5)
                        mail_state = p(6) or "TX"
                        mail_zip   = p(7)
                        site_addr  = p(9)  if len(parts) > 9  else ""
                        site_city  = p(10) if len(parts) > 10 else ""
                        site_zip   = p(12) if len(parts) > 12 else ""

                    if not owner:
                        continue
                    entry = {
                        "prop_address": site_addr,
                        "prop_city":    site_city,
                        "prop_state":   "TX",
                        "prop_zip":     site_zip,
                        "mail_address": mail_addr,
                        "mail_city":    mail_city,
                        "mail_state":   mail_state,
                        "mail_zip":     mail_zip,
                    }
                    for v in _variants(owner):
                        self._idx.setdefault(v, []).append(entry)
                    count += 1
                except Exception:
                    continue

            self.loaded = True
            log.info("Parcel DB (TXT): %d records, %d owner keys", count, len(self._idx))
        except Exception as e:
            log.error("TXT load error: %s", e)

    def _ingest(self, r):
        def g(*keys):
            for k in keys:
                for kk in [k, k.upper(), k.lower()]:
                    v = r.get(kk)
                    if v: return str(v).strip()
            return ""
        owner = g("OWNER","OWN1","OWNERNAME")
        if not owner: return
        entry = {
            "prop_address": g("SITE_ADDR","SITEADDR"),
            "prop_city":    g("SITE_CITY","SITECITY"),
            "prop_state":   "TX",
            "prop_zip":     g("SITE_ZIP","SITEZIP"),
            "mail_address": g("ADDR_1","MAILADR1","MAIL_ADDR"),
            "mail_city":    g("CITY","MAILCITY"),
            "mail_state":   g("STATE","MAILSTATE") or "TX",
            "mail_zip":     g("ZIP","MAILZIP"),
        }
        for v in _variants(owner):
            self._idx.setdefault(v, []).append(entry)

    def lookup(self, name):
        for v in _variants(name or ""):
            h = self._idx.get(v)
            if h: return h[0]
        return None


# ── Scoring ──────────────────────────────────────────────────────────────────

def flags_for(r):
    f, cat, dt, owner, amt, filed = [], r.get("cat",""), r.get("doc_type","").upper(), r.get("owner",""), r.get("amount") or 0, r.get("filed","")
    if cat == "LP":    f.append("Lis pendens")
    if cat == "NOFC":  f.append("Pre-foreclosure")
    if cat == "JUD":   f.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED","TAXDEED"): f.append("Tax lien")
    if dt == "LNMECH": f.append("Mechanic lien")
    if cat == "PRO":   f.append("Probate / estate")
    if re.search(r"\b(LLC|CORP|INC|LTD|LP|GP|TRUST|ASSOC)\b", owner.upper()): f.append("LLC / corp owner")
    try:
        if datetime.strptime(filed[:10], "%Y-%m-%d") >= datetime.now() - timedelta(days=LOOKBACK_DAYS):
            f.append("New this week")
    except Exception: pass
    return list(dict.fromkeys(f))

def score_for(r, flags):
    s = 30
    fw = {"Lis pendens":10,"Pre-foreclosure":10,"Judgment lien":10,"Tax lien":10,
          "Mechanic lien":10,"Probate / estate":10,"LLC / corp owner":10,"New this week":5}
    for f in flags: s += fw.get(f, 0)
    if "Lis pendens" in flags and "Pre-foreclosure" in flags: s += 20
    amt = r.get("amount") or 0
    if amt > 100_000: s += 15
    elif amt > 50_000: s += 10
    if r.get("prop_address") or r.get("mail_address"): s += 5
    return min(s, 100)


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_amt(t):
    try: return float(re.sub(r"[^\d.]", "", str(t).replace(",","")))
    except: return 0.0

def parse_date(t):
    for fmt in ("%m/%d/%Y","%Y-%m-%d","%m-%d-%Y","%B %d, %Y","%b %d, %Y"):
        try: return datetime.strptime(str(t).strip()[:20], fmt).strftime("%Y-%m-%d")
        except: pass
    return str(t)[:10]

def best_table(soup):
    for pat in [r"result",r"grid",r"search",r"record"]:
        t = soup.find("table", {"id": re.compile(pat,re.I)}) or soup.find("table", {"class": re.compile(pat,re.I)})
        if t and len(t.find_all("tr")) > 1: return t
    best, bn = None, 0
    for t in soup.find_all("table"):
        n = len(t.find_all("tr"))
        if n > bn: bn, best = n, t
    return best if bn > 2 else None

def col(cells, headers, *names):
    for name in names:
        for i, h in enumerate(headers):
            if name in h and i < len(cells):
                v = cells[i].get_text(strip=True)
                if v: return v
    return ""

def make_rec(cells, headers, doc_type, cat, cat_label):
    doc_num = col(cells, headers, "FILE","DOC","INSTRUMENT","NUMBER","FILM")
    filed   = col(cells, headers, "DATE","FILED","RECORD")
    grantor = col(cells, headers, "GRANTOR","OWNER","FROM","SELLER","NAME")
    grantee = col(cells, headers, "GRANTEE","TO","BUYER","LENDER")
    legal   = col(cells, headers, "LEGAL","DESCRIPTION","SUBDIV","ABSTRACT")
    amt_s   = col(cells, headers, "AMOUNT","AMT","VALUE","CONSIDER")
    dtype   = col(cells, headers, "TYPE","INSTRUMENT") or doc_type
    if not doc_num and not grantor: return None
    clerk_url = ""
    for td in cells:
        a = td.find("a", href=True)
        if a:
            h = a["href"]
            if h.startswith("http"): clerk_url = h
            elif h.startswith("/"): clerk_url = "https://www.cclerk.hctx.net" + h
            if clerk_url: break
    if not clerk_url and doc_num:
        clerk_url = f"https://www.cclerk.hctx.net/Applications/WebSearch/RP.aspx?FileID={doc_num}"
    return {"doc_num":doc_num,"doc_type":dtype,"filed":parse_date(filed),"cat":cat,"cat_label":cat_label,
            "owner":grantor,"grantee":grantee,"amount":parse_amt(amt_s),"legal":legal,
            "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
            "mail_address":"","mail_city":"","mail_state":"","mail_zip":"",
            "clerk_url":clerk_url,"flags":[],"score":0}


# ── Playwright scraper ───────────────────────────────────────────────────────

async def fill_field(page, selectors, value):
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(value, timeout=3000)
                return True
        except Exception: pass
    return False

async def scrape_type(page, doc_type, start_date, end_date):
    cat, cat_label = DOC_TYPE_MAP.get(doc_type, ("OTHER", doc_type))
    records = []
    log.info("  %s …", doc_type)

    try:
        await page.goto(CLERK_RP_URL, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception as e:
        log.warning("  Load failed %s: %s", doc_type, e)
        return records

    # Fill date from
    await fill_field(page, [
        "input[id*='DateFrom']","input[id*='dateFrom']","input[name*='DateFrom']",
        "input[id*='dfDate']","input[id*='StartDate']","input[id*='txtFrom']",
        "input[id*='dFrom']","[placeholder*='From' i]",
    ], start_date)

    # Fill date to
    await fill_field(page, [
        "input[id*='DateTo']","input[id*='dateTo']","input[name*='DateTo']",
        "input[id*='dtDate']","input[id*='EndDate']","input[id*='txtTo']",
        "input[id*='dTo']","[placeholder*='To' i]",
    ], end_date)

    # Select instrument type
    it_sels = [
        "select[id*='InstrumentType']","select[id*='instrumentType']",
        "select[name*='InstrumentType']","select[id*='ddlIT']",
        "select[id*='Type']","select[id*='type']",
    ]
    for sel in it_sels:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                try: await el.select_option(value=doc_type, timeout=3000); break
                except:
                    try: await el.select_option(label=doc_type, timeout=3000); break
                    except: pass
        except: pass

    # Click search
    for sel in ["input[type='submit']","button[type='submit']","input[value='Search']",
                "input[id*='Search']","button:has-text('Search')","input[value*='search' i]"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=5000)
                break
        except: pass

    try:
        await page.wait_for_load_state("networkidle", timeout=25_000)
    except: pass
    await asyncio.sleep(2)

    # Parse pages
    for page_num in range(1, 51):
        content = await page.content()
        soup    = BeautifulSoup(content, "lxml")
        txt     = soup.get_text().lower()

        if any(p in txt for p in ["no records found","no results","0 records","no matching"]):
            break

        table = best_table(soup)
        if not table: break

        rows = table.find_all("tr")
        if len(rows) < 2: break
        headers = [c.get_text(strip=True).upper() for c in rows[0].find_all(["th","td"])]

        new_n = 0
        for tr in rows[1:]:
            cells = tr.find_all("td")
            if not cells: continue
            try:
                rec = make_rec(cells, headers, doc_type, cat, cat_label)
                if rec:
                    records.append(rec)
                    new_n += 1
            except: pass

        log.info("    page %d: %d rows", page_num, new_n)
        if new_n == 0: break

        # Next page button
        try:
            await page.click("a:has-text('Next'), input[value='Next'], a[id*='Next']", timeout=4000)
            await page.wait_for_load_state("networkidle", timeout=20_000)
            await asyncio.sleep(1)
        except: break

    log.info("  → %d for %s", len(records), doc_type)
    return records


async def scrape_foreclosures(page, start_date, end_date):
    records = []
    log.info("  Foreclosure page …")
    try:
        await page.goto(CLERK_FRCL_URL, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception as e:
        log.warning("  FRCL load failed: %s", e); return records

    content = await page.content()
    soup    = BeautifulSoup(content, "lxml")
    table   = best_table(soup)
    if not table: return records

    rows = table.find_all("tr")
    if len(rows) < 2: return records
    headers = [c.get_text(strip=True).upper() for c in rows[0].find_all(["th","td"])]

    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    for tr in rows[1:]:
        cells = tr.find_all("td")
        if not cells: continue
        try:
            rec = make_rec(cells, headers, "NOFC", "NOFC", "Notice of Foreclosure")
            if rec and (not rec["filed"] or rec["filed"] >= cutoff):
                records.append(rec)
        except: pass

    log.info("  → %d foreclosures", len(records))
    return records


async def scrape_all(start_date, end_date):
    if not HAS_PLAYWRIGHT:
        log.error("Playwright unavailable."); return []

    all_records = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":800},
        )
        ctx.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        page = await ctx.new_page()

        # Warm up session
        try:
            await page.goto("https://www.cclerk.hctx.net/Applications/WebSearch/Home.aspx",
                            wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2)
        except: pass

        for doc_type in DOC_TYPE_MAP:
            if doc_type == "NOFC": continue
            for attempt in range(1, 4):
                try:
                    recs = await scrape_type(page, doc_type, start_date, end_date)
                    all_records.extend(recs)
                    await asyncio.sleep(1.5)
                    break
                except Exception as e:
                    log.warning("Attempt %d for %s: %s", attempt, doc_type, e)
                    await asyncio.sleep(3 * attempt)

        try:
            all_records.extend(await scrape_foreclosures(page, start_date, end_date))
        except Exception as e:
            log.warning("Foreclosure error: %s", e)

        await browser.close()
    return all_records


# ── HCAD download ────────────────────────────────────────────────────────────

HCAD_CACHE_PATH = Path("data/hcad_owner.zip")
HCAD_CACHE_MAX_AGE_DAYS = 7  # re-download once a week


def _cache_is_fresh() -> bool:
    """Return True if the cached ZIP exists and is less than 7 days old."""
    if not HCAD_CACHE_PATH.exists():
        return False
    age = time.time() - HCAD_CACHE_PATH.stat().st_mtime
    return age < (HCAD_CACHE_MAX_AGE_DAYS * 86400)


def download_parcel_db(session: requests.Session) -> ParcelDB:
    db = ParcelDB()

    # ── Use cached file if fresh ────────────────────────────────────────────
    if _cache_is_fresh():
        log.info("Using cached HCAD parcel file (%s)", HCAD_CACHE_PATH)
        try:
            db.load_zip(HCAD_CACHE_PATH.read_bytes())
            if db.loaded:
                return db
        except Exception as e:
            log.warning("Cache load failed: %s — will re-download.", e)

    # ── Download fresh copy ─────────────────────────────────────────────────
    all_urls = HCAD_TXT_URLS + HCAD_DBF_URLS
    for url in all_urls:
        log.info("HCAD downloading: %s", url)
        for attempt in range(1, 4):
            try:
                r = session.get(url, timeout=300, stream=True)
                if r.status_code == 200:
                    log.info("  Download complete — saving to cache…")
                    HCAD_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    HCAD_CACHE_PATH.write_bytes(r.content)
                    db.load_zip(r.content)
                    if db.loaded:
                        log.info("  Parcel DB ready. Cache saved → %s", HCAD_CACHE_PATH)
                        return db
                    break
                elif r.status_code == 404:
                    log.warning("  404: %s", url)
                    break
                else:
                    log.warning("  HTTP %d for %s", r.status_code, url)
                    break
            except Exception as e:
                log.warning("  Attempt %d failed: %s", attempt, e)
                time.sleep(2 ** attempt)

    log.warning("No parcel data available — records saved without addresses.")
    return db


# ── Pipeline ─────────────────────────────────────────────────────────────────

def _normalize_for_lookup(name: str) -> list[str]:
    # Generate multiple lookup attempts for a name
    name = name.strip().upper()
    attempts = [name]
    # Remove common suffixes
    for suffix in [" LLC"," INC"," CORP"," LTD"," LP"," GP"," TRUST"," ESTATE"," ET AL"," JR"," SR"," II"," III"]:
        if name.endswith(suffix):
            attempts.append(name[:-len(suffix)].strip())
    # Try without middle initial: SMITH J D -> SMITH J, SMITH
    parts = name.split()
    if len(parts) >= 3:
        attempts.append(f"{parts[0]} {parts[1]}")
        attempts.append(parts[0])
    return list(dict.fromkeys(a for a in attempts if a))


def enrich(records, db):
    n = 0
    for r in records:
        owner = r.get("owner","").strip()
        if not owner:
            continue
        hit = None
        for attempt in _normalize_for_lookup(owner):
            hit = db.lookup(attempt)
            if hit:
                break
        if hit:
            for k, v in hit.items():
                if v: r[k] = v
            n += 1
    log.info("Enriched %d/%d", n, len(records))
    return records

def apply_scores(records):
    for r in records:
        f = flags_for(r)
        r["flags"] = f
        r["score"] = score_for(r, f)
    return records

def dedupe(records):
    seen, out = set(), []
    for r in records:
        k = r.get("doc_num") or f"{r.get('owner')}|{r.get('filed')}"
        if k and k not in seen:
            seen.add(k); out.append(r)
    return out


# ── Save ─────────────────────────────────────────────────────────────────────

def save_json(records, s, e):
    payload = {
        "fetched_at":   datetime.now(timezone.utc).isoformat(),
        "source":       "Harris County Clerk – Real Property Records",
        "date_range":   {"from": s, "to": e},
        "total":        len(records),
        "with_address": sum(1 for r in records if r.get("prop_address") or r.get("mail_address")),
        "records":      records,
    }
    for p in OUTPUT_PATHS:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        log.info("Saved → %s", p)

def save_csv(records):
    GHL_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GHL_CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=GHL_COLUMNS)
        w.writeheader()
        for r in records:
            pts = (r.get("owner") or "").strip().split()
            fn = pts[0] if pts else ""; ln = " ".join(pts[1:]) if len(pts) > 1 else ""
            w.writerow({
                "First Name":fn,"Last Name":ln,
                "Mailing Address":r.get("mail_address",""),"Mailing City":r.get("mail_city",""),
                "Mailing State":r.get("mail_state",""),"Mailing Zip":r.get("mail_zip",""),
                "Property Address":r.get("prop_address",""),"Property City":r.get("prop_city",""),
                "Property State":r.get("prop_state","TX"),"Property Zip":r.get("prop_zip",""),
                "Lead Type":r.get("cat_label",""),"Document Type":r.get("doc_type",""),
                "Date Filed":r.get("filed",""),"Document Number":r.get("doc_num",""),
                "Amount/Debt Owed":r.get("amount",""),"Seller Score":r.get("score",0),
                "Motivated Seller Flags":"; ".join(r.get("flags",[])),"Source":"Harris County Clerk",
                "Public Records URL":r.get("clerk_url",""),
            })
    log.info("GHL CSV → %s", GHL_CSV_PATH)


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    start    = start_dt.strftime("%m/%d/%Y")
    end      = end_dt.strftime("%m/%d/%Y")
    iso_s    = start_dt.strftime("%Y-%m-%d")
    iso_e    = end_dt.strftime("%Y-%m-%d")

    log.info("="*60)
    log.info("Harris County Lead Scraper — %s to %s", start, end)
    log.info("="*60)

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"

    parcel_db = download_parcel_db(session)
    records   = await scrape_all(start, end)
    log.info("Raw: %d", len(records))

    records = dedupe(records)
    records = enrich(records, parcel_db)
    records = apply_scores(records)
    records.sort(key=lambda r: r.get("score",0), reverse=True)

    save_json(records, iso_s, iso_e)
    save_csv(records)

    log.info("="*60)
    log.info("Done. %d leads. Top score: %d", len(records), records[0]["score"] if records else 0)
    log.info("="*60)

if __name__ == "__main__":
    asyncio.run(main())
