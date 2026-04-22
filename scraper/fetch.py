"""
Harris County Motivated Seller Lead Scraper
- Playwright scraper for Harris County Clerk RP.aspx
- HCAD address lookup from real_acct.txt + owners.txt loaded from Google Drive
"""
from __future__ import annotations
import asyncio, csv, io, json, logging, os, re, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))
CLERK_BASE    = "https://www.cclerk.hctx.net/Applications/WebSearch/RP.aspx"
CLERK_FRCL    = "https://www.cclerk.hctx.net/Applications/WebSearch/FRCL_R.aspx"
CLERK_HOME    = "https://www.cclerk.hctx.net/Applications/WebSearch/Home.aspx"

# Google Drive direct download — real_acct.txt (public file)
GDRIVE_REAL_ACCT_ID = os.getenv("GDRIVE_REAL_ACCT_ID",
                                 "1CwLnPOw1HlzuKpG4iuBIcqBv6XU_g4hy")
GDRIVE_OWNERS_ID    = os.getenv("GDRIVE_OWNERS_ID", "")

# Local cache paths
CACHE_DIR        = Path("data")
REAL_ACCT_CACHE  = CACHE_DIR / "real_acct.txt"
OWNERS_CACHE     = CACHE_DIR / "owners.txt"
CACHE_MAX_DAYS   = 7

OUTPUT_PATHS  = [Path("dashboard/records.json"), Path("data/records.json")]
GHL_CSV_PATH  = Path("data/ghl_export.csv")
GHL_COLUMNS   = [
    "First Name","Last Name","Mailing Address","Mailing City","Mailing State","Mailing Zip",
    "Property Address","Property City","Property State","Property Zip",
    "Lead Type","Document Type","Date Filed","Document Number","Amount/Debt Owed",
    "Seller Score","Motivated Seller Flags","Source","Public Records URL",
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

# ── HCAD Parcel DB ────────────────────────────────────────────────────────────
# real_acct.txt columns (tab-delimited):
# 0:acct  2:mailto(owner name)  3:mail_addr_1  5:mail_city  6:mail_state
# 7:mail_zip  10:str_pfx  11:str_num  13:str  14:str_sfx  16:str_unit
# 17:site_addr_1

def _norm(s): return re.sub(r"\s+", " ", str(s).strip().upper())

def _variants(name):
    n = _norm(name); p = n.split(); v = [n]
    if len(p) >= 2:
        v += [f"{p[-1]} {' '.join(p[:-1])}", f"{p[-1]}, {' '.join(p[:-1])}"]
    if len(p) >= 3:
        v.append(f"{p[0]} {p[1]}")
    # Without common suffixes
    clean = re.sub(r"\b(LLC|INC|CORP|LTD|LP|GP|TRUST|ESTATE|ET AL|JR|SR|II|III)\b","",n).strip()
    if clean != n:
        v += _variants(clean)
    return list(dict.fromkeys(v for v in v if v))


class ParcelDB:
    def __init__(self): self._by_name = {}; self._by_acct = {}; self.loaded = False

    def load_real_acct(self, path: Path) -> None:
        if not path.exists():
            log.warning("real_acct.txt not found at %s", path)
            return
        log.info("Loading real_acct.txt (%s MB) ...",
                 round(path.stat().st_size / 1_000_000, 1))
        count = 0
        try:
            with path.open(encoding="latin-1") as f:
                # Skip header
                header_line = f.readline()
                cols = header_line.strip().split("\t")
                # Map column names to indices
                def ci(name): return cols.index(name) if name in cols else -1
                i_acct  = ci("acct")
                i_name  = ci("mailto")       # owner name
                i_ma1   = ci("mail_addr_1")
                i_mc    = ci("mail_city")
                i_ms    = ci("mail_state")
                i_mz    = ci("mail_zip")
                i_spfx  = ci("str_pfx")
                i_snum  = ci("str_num")
                i_str   = ci("str")
                i_ssfx  = ci("str_sfx")
                i_sunit = ci("str_unit")
                i_site1 = ci("site_addr_1")
                log.info("Column indices — acct:%d name:%d mail:%d site:%d",
                         i_acct, i_name, i_ma1, i_site1)

                for line in f:
                    if not line.strip(): continue
                    p = line.split("\t")
                    def g(i): return p[i].strip() if i >= 0 and i < len(p) else ""

                    acct  = g(i_acct)
                    owner = g(i_name)

                    # Build site address from components
                    site_addr = g(i_site1)
                    if not site_addr:
                        parts = [g(i_snum), g(i_spfx), g(i_str), g(i_ssfx), g(i_sunit)]
                        site_addr = " ".join(x for x in parts if x).strip()

                    entry = {
                        "prop_address": site_addr,
                        "prop_city":    "Houston",
                        "prop_state":   "TX",
                        "prop_zip":     "",
                        "mail_address": g(i_ma1),
                        "mail_city":    g(i_mc),
                        "mail_state":   g(i_ms) or "TX",
                        "mail_zip":     g(i_mz),
                    }

                    if acct:
                        self._by_acct[acct] = entry
                    if owner:
                        for v in _variants(owner):
                            self._by_name.setdefault(v, []).append(entry)
                    count += 1

            self.loaded = True
            log.info("ParcelDB loaded: %d records, %d name keys, %d acct keys",
                     count, len(self._by_name), len(self._by_acct))
        except Exception as e:
            log.error("real_acct.txt load error: %s", e)

    def load_owners(self, path: Path) -> None:
        """Load owners.txt to supplement name->acct mapping."""
        if not path.exists(): return
        try:
            with path.open(encoding="latin-1") as f:
                cols = f.readline().strip().split("\t")
                ia = cols.index("acct") if "acct" in cols else 0
                iname = cols.index("name") if "name" in cols else 2
                for line in f:
                    p = line.split("\t")
                    if len(p) <= max(ia, iname): continue
                    acct  = p[ia].strip()
                    owner = p[iname].strip()
                    if owner and acct and acct in self._by_acct:
                        for v in _variants(owner):
                            if v not in self._by_name:
                                self._by_name[v] = [self._by_acct[acct]]
            log.info("Owners supplement loaded. Name keys now: %d", len(self._by_name))
        except Exception as e:
            log.warning("owners.txt load error: %s", e)

    def lookup(self, name: str) -> dict | None:
        if not name: return None
        for v in _variants(name):
            h = self._by_name.get(v)
            if h: return h[0]
        return None


def _cache_fresh(path: Path) -> bool:
    if not path.exists(): return False
    return (time.time() - path.stat().st_mtime) < (CACHE_MAX_DAYS * 86400)


def download_gdrive_file(session: requests.Session, file_id: str,
                          dest: Path) -> bool:
    """Download a large file from Google Drive, handling the virus scan page."""
    if not file_id:
        return False
    log.info("Downloading from Google Drive (id=%s) ...", file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: Initial request
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        r = session.get(url, timeout=60, allow_redirects=True)
        log.info("  GDrive initial response: HTTP %d, %d bytes",
                 r.status_code, len(r.content))

        # Step 2: Check if we got a confirmation page (large file warning)
        if b"virus scan warning" in r.content.lower() or            b"download_warning" in r.content or            b"confirm" in r.content[:2000]:
            log.info("  Got confirmation page — extracting token ...")
            # Try to find confirm token
            token = re.search(rb'confirm=([^&"]+)', r.content)
            uuid  = re.search(rb'uuid=([^&"]+)', r.content)
            if token:
                confirm = token.group(1).decode()
                dl_url  = f"https://drive.google.com/uc?export=download&confirm={confirm}&id={file_id}"
                log.info("  Downloading with confirm token ...")
                r = session.get(dl_url, timeout=600, stream=True)
            elif uuid:
                uid    = uuid.group(1).decode()
                dl_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t&uuid={uid}"
                log.info("  Downloading with uuid ...")
                r = session.get(dl_url, timeout=600, stream=True)
            else:
                # Try the usercontent endpoint directly
                dl_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t"
                log.info("  Trying usercontent endpoint ...")
                r = session.get(dl_url, timeout=600, stream=True)

        # Step 3: Check content type — must be text not HTML
        content_type = r.headers.get("content-type","")
        log.info("  Content-Type: %s", content_type)

        if "text/html" in content_type:
            # Still getting HTML — save first 500 chars for debugging
            preview = r.content[:500].decode("utf-8", errors="replace")
            log.error("  Got HTML instead of file: %s", preview[:200])
            return False

        # Step 4: Save file
        if r.status_code == 200:
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
            size_mb = round(dest.stat().st_size / 1_000_000, 1)
            log.info("  Saved %s MB → %s", size_mb, dest)
            return size_mb > 1  # Must be at least 1MB to be valid
        else:
            log.warning("  HTTP %d", r.status_code)
            return False

    except Exception as e:
        log.error("  GDrive download error: %s", e)
        return False


def load_parcel_db(session: requests.Session) -> ParcelDB:
    db = ParcelDB()

    # Try to get real_acct.txt
    if _cache_fresh(REAL_ACCT_CACHE):
        log.info("Using cached real_acct.txt")
    elif GDRIVE_REAL_ACCT_ID:
        download_gdrive_file(session, GDRIVE_REAL_ACCT_ID, REAL_ACCT_CACHE)
    else:
        log.warning("No GDRIVE_REAL_ACCT_ID set — skipping real_acct.txt download.")
        log.warning("Set this in GitHub Actions secrets to enable address enrichment.")

    db.load_real_acct(REAL_ACCT_CACHE)

    # Supplement with owners.txt
    if _cache_fresh(OWNERS_CACHE):
        db.load_owners(OWNERS_CACHE)
    elif GDRIVE_OWNERS_ID:
        download_gdrive_file(session, GDRIVE_OWNERS_ID, OWNERS_CACHE)
        db.load_owners(OWNERS_CACHE)

    return db


# ── Scoring ──────────────────────────────────────────────────────────────────

def flags_for(r):
    f=[]; cat=r.get("cat",""); dt=r.get("doc_type","").upper()
    owner=r.get("owner",""); filed=r.get("filed","")
    if cat=="LP":    f.append("Lis pendens")
    if cat=="NOFC":  f.append("Pre-foreclosure")
    if cat=="JUD":   f.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED","TAXDEED"): f.append("Tax lien")
    if dt=="LNMECH": f.append("Mechanic lien")
    if cat=="PRO":   f.append("Probate / estate")
    if re.search(r"\b(LLC|CORP|INC|LTD|LP|GP|TRUST|ASSOC)\b", owner.upper()):
        f.append("LLC / corp owner")
    try:
        if datetime.strptime(filed[:10],"%Y-%m-%d") >= datetime.now()-timedelta(days=LOOKBACK_DAYS):
            f.append("New this week")
    except: pass
    return list(dict.fromkeys(f))

def score_for(r, flags):
    s=30
    fw={"Lis pendens":10,"Pre-foreclosure":10,"Judgment lien":10,"Tax lien":10,
        "Mechanic lien":10,"Probate / estate":10,"LLC / corp owner":10,"New this week":5}
    for f in flags: s+=fw.get(f,0)
    if "Lis pendens" in flags and "Pre-foreclosure" in flags: s+=20
    amt=r.get("amount") or 0
    if amt>100_000: s+=15
    elif amt>50_000: s+=10
    if r.get("prop_address") or r.get("mail_address"): s+=5
    return min(s,100)

# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_amt(t):
    try: return float(re.sub(r"[^\d.]","",str(t).replace(",","")))
    except: return 0.0

def parse_date(t):
    for fmt in ("%m/%d/%Y","%Y-%m-%d","%m-%d-%Y","%B %d, %Y","%b %d, %Y"):
        try: return datetime.strptime(str(t).strip()[:20],fmt).strftime("%Y-%m-%d")
        except: pass
    return str(t)[:10]

def best_table(soup):
    for pat in [r"result",r"grid",r"search",r"record",r"data"]:
        t=(soup.find("table",{"id":re.compile(pat,re.I)}) or
           soup.find("table",{"class":re.compile(pat,re.I)}))
        if t and len(t.find_all("tr"))>1: return t
    best,bn=None,0
    for t in soup.find_all("table"):
        n=len(t.find_all("tr"))
        if n>bn: bn,best=n,t
    return best if bn>2 else None

def clean_grantor(raw):
    if not raw: return ""
    if "Grantor:" in raw or "Grantee:" in raw:
        names=re.findall(r"Grantor:([^G]+?)(?=Grantor:|Grantee:|$)",raw)
        if names:
            skip={"BANK","TRUST","CORP","LLC","INC","MORTGAGE","ELECTRONIC",
                  "REGISTRATION","SYSTEMS","SERIES","NATIONAL","FEDERAL","AMERICA"}
            for n in names:
                n=n.strip()
                if n and not any(s in n.upper() for s in skip): return n
            return names[0].strip()
    return raw.strip()

def clean_grantee(raw):
    if not raw: return ""
    if "Grantee:" in raw:
        m=re.search(r"Grantee:([^G]+?)(?=Grantor:|Grantee:|$)",raw)
        if m: return m.group(1).strip()
    return raw.strip()

def parse_row(cells, headers, doc_type, cat, cat_label):
    def col(*names):
        for nm in names:
            for i,h in enumerate(headers):
                if nm in h and i<len(cells):
                    v=cells[i].get_text(strip=True)
                    if v: return v
        return ""
    doc_num=col("FILE","DOC","INSTRUMENT","NUMBER","FILM")
    filed  =col("DATE","FILED","RECORD")
    grantor=col("GRANTOR","OWNER","FROM","SELLER","NAME")
    grantee=col("GRANTEE","TO","BUYER","LENDER")
    legal  =col("LEGAL","DESCRIPTION","SUBDIV","ABSTRACT")
    amt_s  =col("AMOUNT","AMT","VALUE","CONSIDER")
    dtype  =col("TYPE","INSTRUMENT") or doc_type
    if "Grantor:" in grantor or "Grantee:" in grantor:
        if not grantee: grantee=clean_grantee(grantor)
        grantor=clean_grantor(grantor)
    if "Grantor:" in legal: legal=""
    if not doc_num and not grantor: return None
    clerk_url=""
    for td in cells:
        a=td.find("a",href=True)
        if a:
            h=a["href"]
            if h.startswith("http"): clerk_url=h
            elif h.startswith("/"): clerk_url="https://www.cclerk.hctx.net"+h
            if clerk_url: break
    if not clerk_url and doc_num:
        clerk_url=f"{CLERK_BASE}?FileID={doc_num}"
    return {"doc_num":doc_num,"doc_type":dtype,"filed":parse_date(filed),
            "cat":cat,"cat_label":cat_label,"owner":grantor,"grantee":grantee,
            "amount":parse_amt(amt_s),"legal":legal,
            "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
            "mail_address":"","mail_city":"","mail_state":"","mail_zip":"",
            "clerk_url":clerk_url,"flags":[],"score":0}

# ── Playwright ────────────────────────────────────────────────────────────────

async def get_page_records(page, doc_type, cat, cat_label):
    records=[]
    content=await page.content()
    soup=BeautifulSoup(content,"lxml")
    txt=soup.get_text().lower()
    if any(p in txt for p in ["no records found","no results","0 records",
                               "no matching","search returned no","no data"]):
        return records
    table=best_table(soup)
    if not table: return records
    rows=table.find_all("tr")
    if len(rows)<2: return records
    headers=[c.get_text(strip=True).upper() for c in rows[0].find_all(["th","td"])]
    for tr in rows[1:]:
        cells=tr.find_all("td")
        if not cells: continue
        try:
            rec=parse_row(cells,headers,doc_type,cat,cat_label)
            if rec: records.append(rec)
        except: pass
    return records


async def scrape_type(page, doc_type, start_date, end_date):
    cat,cat_label=DOC_TYPE_MAP.get(doc_type,("OTHER",doc_type))
    records=[]
    log.info("  %s ...", doc_type)
    try:
        await page.goto(CLERK_BASE, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await asyncio.sleep(1)
    except Exception as e:
        log.warning("  Nav error %s: %s", doc_type, e); return records

    # Dump form elements on first type for debugging
    if doc_type == "LP":
        try:
            inputs = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('input,select,button'))
                .filter(e => e.offsetParent !== null)
                .map(e => ({tag:e.tagName,id:e.id,name:e.name,
                            type:e.type||'',placeholder:e.placeholder||''}))
                .filter(e => e.id || e.name);
            }""")
            log.info("  FORM_FIELDS: %s", json.dumps(inputs))
            body = await page.evaluate("() => document.body.innerText.substring(0,300)")
            log.info("  PAGE_BODY: %s", body.replace('\n',' '))
        except Exception as e:
            log.warning("  Debug dump error: %s", e)

    # Fill date fields — try every known pattern
    date_filled = False
    for df_sel, dt_sel in [
        ("input[id*='DateFrom']",   "input[id*='DateTo']"),
        ("input[id*='dateFrom']",   "input[id*='dateTo']"),
        ("input[name*='DateFrom']", "input[name*='DateTo']"),
        ("input[id*='StartDate']",  "input[id*='EndDate']"),
        ("input[id*='dFrom']",      "input[id*='dTo']"),
        ("input[id*='txtFrom']",    "input[id*='txtTo']"),
        ("input[id*='BeginDate']",  "input[id*='EndDate']"),
    ]:
        try:
            df = page.locator(df_sel).first
            dt = page.locator(dt_sel).first
            if await df.count() > 0 and await dt.count() > 0:
                await df.fill(start_date, timeout=3000)
                await dt.fill(end_date, timeout=3000)
                date_filled = True
                log.info("  Dates filled: %s / %s", df_sel, dt_sel)
                break
        except: pass

    # Fallback: fill first two visible text inputs
    if not date_filled:
        try:
            vis = []
            for inp in await page.query_selector_all("input[type='text'],input:not([type])"):
                if await inp.is_visible():
                    vis.append(inp)
            if len(vis) >= 2:
                await vis[0].fill(start_date)
                await vis[1].fill(end_date)
                date_filled = True
                log.info("  Dates filled via positional fallback")
        except: pass

    if not date_filled:
        log.warning("  Could not fill dates for %s", doc_type)

    # Set instrument type via JavaScript — most reliable
    try:
        result = await page.evaluate(f"""() => {{
            const selects = document.querySelectorAll('select');
            for (const s of selects) {{
                for (const o of s.options) {{
                    if (o.value==='{doc_type}' || o.text.trim()==='{doc_type}') {{
                        s.value = o.value;
                        s.dispatchEvent(new Event('change',{{bubbles:true}}));
                        return 'OK:'+s.id+'='+o.value;
                    }}
                }}
            }}
            // Log all options for debugging
            const opts = [];
            document.querySelectorAll('select option').forEach(o=>opts.push(o.value+'|'+o.text));
            return 'NOT_FOUND. Options: '+opts.slice(0,20).join(', ');
        }}""")
        log.info("  InstrumentType JS: %s", result)
    except Exception as e:
        log.warning("  JS select error: %s", e)

    # Submit
    for sel in ["input[type='submit']","button[type='submit']",
                "input[value='Search']","button:has-text('Search')",
                "input[id*='earch']","button[id*='earch']"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=5000)
                log.info("  Submitted via: %s", sel)
                break
        except: pass

    try:
        await page.wait_for_load_state("networkidle", timeout=25_000)
    except: pass
    await asyncio.sleep(2)

    for pg in range(1, 51):
        recs = await get_page_records(page, doc_type, cat, cat_label)
        if not recs: break
        records.extend(recs)
        log.info("  Page %d: %d rows", pg, len(recs))
        try:
            await page.click("a:has-text('Next'),input[value='Next'],a[id*='Next']",
                             timeout=4000)
            await page.wait_for_load_state("networkidle", timeout=20_000)
            await asyncio.sleep(1)
        except: break

    log.info("  → %d for %s", len(records), doc_type)
    return records


async def scrape_foreclosures(page):
    records=[]
    log.info("  Foreclosures ...")
    try:
        await page.goto(CLERK_FRCL,wait_until="domcontentloaded",timeout=45_000)
        await page.wait_for_load_state("networkidle",timeout=20_000)
        await asyncio.sleep(2)
    except Exception as e:
        log.warning("  FRCL error: %s",e); return records
    content=await page.content()
    soup=BeautifulSoup(content,"lxml")
    table=best_table(soup)
    if not table: return records
    rows=table.find_all("tr")
    if len(rows)<2: return records
    headers=[c.get_text(strip=True).upper() for c in rows[0].find_all(["th","td"])]
    cutoff=(datetime.now()-timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    for tr in rows[1:]:
        cells=tr.find_all("td")
        if not cells: continue
        try:
            rec=parse_row(cells,headers,"NOFC","NOFC","Notice of Foreclosure")
            if rec and (not rec["filed"] or rec["filed"]>=cutoff):
                records.append(rec)
        except: pass
    log.info("  → %d foreclosures",len(records))
    return records


async def scrape_all(start_date, end_date):
    if not HAS_PLAYWRIGHT:
        log.error("Playwright unavailable"); return []
    all_records=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx=await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":800},
        )
        ctx.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        page=await ctx.new_page()

        log.info("Warming up session ...")
        try:
            await page.goto(CLERK_HOME,wait_until="domcontentloaded",timeout=30_000)
            await page.wait_for_load_state("networkidle",timeout=15_000)
            await asyncio.sleep(3)
            log.info("Session: %s", await page.title())
        except Exception as e:
            log.warning("Warmup: %s",e)

        for doc_type in DOC_TYPE_MAP:
            if doc_type=="NOFC": continue
            for attempt in range(1,4):
                try:
                    recs=await scrape_type(page,doc_type,start_date,end_date)
                    all_records.extend(recs)
                    await asyncio.sleep(1)
                    break
                except Exception as e:
                    log.warning("Attempt %d for %s: %s",attempt,doc_type,e)
                    await asyncio.sleep(3*attempt)

        try:
            all_records.extend(await scrape_foreclosures(page))
        except Exception as e:
            log.warning("Foreclosure: %s",e)

        await browser.close()
    return all_records

# ── Pipeline ─────────────────────────────────────────────────────────────────

def enrich(records, db: ParcelDB):
    n=0
    for r in records:
        owner=r.get("owner","").strip()
        if not owner: continue
        hit=db.lookup(owner)
        if hit:
            for k,v in hit.items():
                if v: r[k]=v
            n+=1
    log.info("Enriched %d/%d records with addresses", n, len(records))
    return records

def dedupe(records):
    seen,out=set(),[]
    for r in records:
        k=r.get("doc_num") or f"{r.get('owner')}|{r.get('filed')}"
        if k and k not in seen:
            seen.add(k); out.append(r)
    return out

def apply_scores(records):
    for r in records:
        f=flags_for(r); r["flags"]=f; r["score"]=score_for(r,f)
    return records

def save_json(records, s, e):
    payload={"fetched_at":datetime.now(timezone.utc).isoformat(),
             "source":"Harris County Clerk – Real Property Records",
             "date_range":{"from":s,"to":e},"total":len(records),
             "with_address":sum(1 for r in records if r.get("prop_address") or r.get("mail_address")),
             "records":records}
    for p in OUTPUT_PATHS:
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(payload,indent=2,ensure_ascii=False))
        log.info("Saved → %s", p)

def save_csv(records):
    GHL_CSV_PATH.parent.mkdir(parents=True,exist_ok=True)
    with GHL_CSV_PATH.open("w",newline="",encoding="utf-8") as fh:
        w=csv.DictWriter(fh,fieldnames=GHL_COLUMNS); w.writeheader()
        for r in records:
            pts=(r.get("owner") or "").strip().split()
            fn=pts[0] if pts else ""; ln=" ".join(pts[1:]) if len(pts)>1 else ""
            w.writerow({"First Name":fn,"Last Name":ln,
                "Mailing Address":r.get("mail_address",""),"Mailing City":r.get("mail_city",""),
                "Mailing State":r.get("mail_state",""),"Mailing Zip":r.get("mail_zip",""),
                "Property Address":r.get("prop_address",""),"Property City":r.get("prop_city",""),
                "Property State":r.get("prop_state","TX"),"Property Zip":r.get("prop_zip",""),
                "Lead Type":r.get("cat_label",""),"Document Type":r.get("doc_type",""),
                "Date Filed":r.get("filed",""),"Document Number":r.get("doc_num",""),
                "Amount/Debt Owed":r.get("amount",""),"Seller Score":r.get("score",0),
                "Motivated Seller Flags":"; ".join(r.get("flags",[])),"Source":"Harris County Clerk",
                "Public Records URL":r.get("clerk_url","")})
    log.info("GHL CSV → %s", GHL_CSV_PATH)

# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    end_dt=datetime.now(); start_dt=end_dt-timedelta(days=LOOKBACK_DAYS)
    start=start_dt.strftime("%m/%d/%Y"); end=end_dt.strftime("%m/%d/%Y")
    iso_s=start_dt.strftime("%Y-%m-%d"); iso_e=end_dt.strftime("%Y-%m-%d")
    log.info("="*60)
    log.info("Harris County Lead Scraper — %s to %s", start, end)
    log.info("="*60)

    session=requests.Session()
    session.headers["User-Agent"]="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"

    # Load parcel data
    parcel_db = load_parcel_db(session)

    # Scrape clerk records
    records=await scrape_all(start, end)
    log.info("Raw: %d", len(records))
    records=dedupe(records)
    records=enrich(records, parcel_db)
    records=apply_scores(records)
    records.sort(key=lambda r: r.get("score",0), reverse=True)
    save_json(records, iso_s, iso_e)
    save_csv(records)
    log.info("="*60)
    log.info("Done. %d leads.", len(records))
    log.info("="*60)

if __name__=="__main__":
    asyncio.run(main())
