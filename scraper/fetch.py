"""Harris County Motivated Seller Lead Scraper"""
from __future__ import annotations
import asyncio, csv, json, logging, os, re, sys, time
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

LOOKBACK_DAYS       = int(os.getenv("LOOKBACK_DAYS", "7"))
CLERK_BASE          = "https://www.cclerk.hctx.net/Applications/WebSearch/RP.aspx"
CLERK_FRCL          = "https://www.cclerk.hctx.net/Applications/WebSearch/FRCL_R.aspx"
CLERK_HOME          = "https://www.cclerk.hctx.net/Applications/WebSearch/Home.aspx"
GDRIVE_REAL_ACCT_ID = os.getenv("GDRIVE_REAL_ACCT_ID", "1CwLnPOw1HlzuKpG4iuBIcqBv6XU_g4hy")
GDRIVE_DEEDS_ID     = os.getenv("GDRIVE_DEEDS_ID",     "1EsmdzaeRb95UB6Ti9m5ANrV3prSzvU5V")
CACHE_DIR           = Path("data")
REAL_ACCT_CACHE     = CACHE_DIR / "real_acct.txt"
DEEDS_CACHE         = CACHE_DIR / "deeds.txt"
CACHE_MAX_DAYS      = 7

OUTPUT_PATHS = [Path("dashboard/records.json"), Path("data/records.json")]
GHL_CSV_PATH = Path("data/ghl_export.csv")
GHL_COLUMNS  = [
    "First Name","Last Name","Mailing Address","Mailing City","Mailing State","Mailing Zip",
    "Property Address","Property City","Property State","Property Zip",
    "Lead Type","Document Type","Date Filed","Document Number","Amount/Debt Owed",
    "Seller Score","Motivated Seller Flags","Source","Public Records URL",
    "Match Confidence","HCAD Lookup URL",
]

DOC_TYPE_MAP = {
    "LP":("LP","Lis Pendens"),"NOFC":("NOFC","Notice of Foreclosure"),
    "TAXDEED":("TAXDEED","Tax Deed"),"JUD":("JUD","Judgment"),
    "CCJ":("JUD","Certified Judgment"),"DRJUD":("JUD","Domestic Judgment"),
    "LNCORPTX":("LIEN","Corp Tax Lien"),"LNIRS":("LIEN","IRS Lien"),
    "LNFED":("LIEN","Federal Lien"),"LN":("LIEN","Lien"),
    "LNMECH":("LIEN","Mechanic Lien"),"LNHOA":("LIEN","HOA Lien"),
    "MEDLN":("LIEN","Medicaid Lien"),"PRO":("PRO","Probate Document"),
    "NOC":("NOC","Notice of Commencement"),"RELLP":("RELLP","Release Lis Pendens"),
}

_UNKNOWN_TYPES: dict = {}

def categorize(dt):
    dt = dt.upper().strip()
    if dt in DOC_TYPE_MAP: return DOC_TYPE_MAP[dt]
    kw = [
        ("T/L",("LIEN","Tax Lien")),
        ("L/P",("LP","Lis Pendens")),("LIS P",("LP","Lis Pendens")),("LP",("LP","Lis Pendens")),
        ("FORECLOS",("NOFC","Notice of Foreclosure")),
        ("TAX DEED",("TAXDEED","Tax Deed")),
        ("DRJUD",("JUD","Domestic Judgment")),("CCJ",("JUD","Certified Judgment")),
        ("A/J",("JUD","Abstract of Judgment")),("ABST",("JUD","Abstract of Judgment")),
        ("AFFT",("JUD","Affidavit")),("L AFFT",("JUD","Affidavit of Lien")),
        ("JUDGMENT",("JUD","Judgment")),("JUD",("JUD","Judgment")),
        ("IRS",("LIEN","IRS Lien")),("FED TAX",("LIEN","Federal Tax Lien")),
        ("FED LN",("LIEN","Federal Lien")),("CORP TAX",("LIEN","Corp Tax Lien")),
        ("MECH",("LIEN","Mechanic Lien")),("HOA",("LIEN","HOA Lien")),
        ("MEDICAID",("LIEN","Medicaid Lien")),("FI STM",("LIEN","Financing Statement")),
        ("ASSGN",("LIEN","Assignment")),("TAX LN",("LIEN","Tax Lien")),
        ("LIEN",("LIEN","Lien")),(" LN",("LIEN","Lien")),
        ("COMMENCE",("NOC","Notice of Commencement")),("NOC",("NOC","Notice of Commencement")),
        ("NOTICE OF F",("NOFC","Notice of Foreclosure")),("NOTICE",("NOC","Notice")),
        ("REL LP",("RELLP","Release Lis Pendens")),("RELLP",("RELLP","Release Lis Pendens")),
        ("PT REL",("RELLP","Release Lis Pendens")),("RELEASE",("RELLP","Release Lis Pendens")),
        ("PROBATE",("PRO","Probate Document")),("PRO",("PRO","Probate Document")),
        ("ST TAX",("LIEN","State Tax Lien")),("FED TAX",("LIEN","Federal Tax Lien")),
        ("CITY TAX",("LIEN","City Tax Lien")),
    ]
    for key, val in kw:
        if key in dt: return val
    _UNKNOWN_TYPES[dt] = _UNKNOWN_TYPES.get(dt, 0) + 1
    return None

def _clerk_variants(raw):
    s = raw.strip().upper().replace(" ", ""); v = {s}
    m = re.match(r'^RP-(\d{4})-(\d+)$', s)
    if m: v.add(f"{m.group(1)}-RP-{m.group(2)}"); v.add(m.group(2).lstrip('0') or '0')
    m2 = re.match(r'^(\d{4})-RP-(\d+)$', s)
    if m2: v.add(f"RP-{m2.group(1)}-{m2.group(2)}"); v.add(m2.group(2).lstrip('0') or '0')
    if s.isdigit(): v.add(s.lstrip('0') or '0')
    return list(v)

def _norm(s): return re.sub(r"\s+", " ", str(s).strip().upper())

def _variants(name):
    n = _norm(name); p = n.split(); v = [n]
    if len(p) >= 2: v += [f"{p[-1]} {' '.join(p[:-1])}", f"{p[-1]}, {' '.join(p[:-1])}"]
    if len(p) >= 3: v.append(f"{p[0]} {p[1]}")
    clean = re.sub(r"\b(LLC|INC|CORP|LTD|LP|GP|TRUST|ESTATE|ET AL|JR|SR|II|III)\b", "", n).strip()
    if clean and clean != n:
        cp = clean.split(); v.append(clean)
        if len(cp) >= 2: v.append(f"{cp[-1]} {' '.join(cp[:-1])}")
    return list(dict.fromkeys(x for x in v if x))

CORP_SKIP = {"BANK","TRUST","CORP","LLC","INC","MORTGAGE","ELECTRONIC","REGISTRATION",
             "SYSTEMS","SERIES","NATIONAL","FEDERAL","AMERICA","FINANCE","CAPITAL",
             "INVESTMENT","SERVICES","FUNDING","ASSOCIATION"}

def _is_person(n):
    return not any(s in n.upper() for s in CORP_SKIP) and len(n.split()) >= 2

def _extract_names(raw):
    if not raw: return "", ""
    # Split on "Grantor :" / "Grantee :" / "Trustee :" labels
    # re.split with a capturing group returns [before, label1, value1, label2, value2, ...]
    parts = re.split(r"(?i)\b(Grantor|Grantee|Trustee)\s*:\s*", raw.strip())
    grantors, grantees = [], []
    i = 1
    while i < len(parts) - 1:
        label = parts[i].upper()
        name  = parts[i + 1].strip()
        if name and name.upper() not in ("SEE INSTRUMENT", "N/A", ""):
            if label == "GRANTOR":
                grantors.append(name)
            else:
                grantees.append(name)
        i += 2
    if not grantors and not grantees:
        lines = [p.strip() for p in raw.split("\n") if p.strip()]
        return lines[0] if lines else "", lines[1] if len(lines) > 1 else ""
    return "; ".join(grantors), "; ".join(grantees)

def _parse_legal(legal):
    if not legal: return {}
    s = legal.upper(); r = {}
    m = re.search(r'DESC[:\s]+([^,\n]+?)(?=\s*SEC[:\s]|\s*LOT[:\s]|\s*BLK[:\s]|$)', s)
    if m: r['sub'] = m.group(1).strip()[:60]
    m = re.search(r'LOT[:\s]+(\w+)', s)
    if m: r['lot'] = m.group(1)
    m = re.search(r'(?:BLK|BLOCK)[:\s]+(\w+)', s)
    if m: r['block'] = m.group(1)
    if not r.get('lot'):
        m = re.search(r'\bLT\s+(\w+)', s)
        if m: r['lot'] = m.group(1)
    if not r.get('sub'):
        sub = re.sub(r'\b(LT|BLK|SEC|TR|ABST|UNIT|PHASE|LOT|BLOCK)\s*[\dA-Z]+', '', s)
        sub = re.sub(r'\s+', ' ', sub).strip(' &,.-')
        if len(sub) > 4: r['sub'] = sub[:60]
    return r

def _legal_key(legal):
    p = _parse_legal(legal)
    if p.get('lot') and p.get('block') and p.get('sub'):
        return f"{p['sub'][:40]}|{p['block']}|{p['lot']}"
    if p.get('lot') and p.get('sub'):
        return f"{p['sub'][:40]}|{p['lot']}"
    return None

class ParcelDB:
    def __init__(self):
        self._by_name = {}; self._by_legal = {}
        self._by_acct = {}; self._by_clerk = {}

    def load_real_acct(self, path):
        if not path.exists(): return
        log.info("Loading real_acct.txt (%s MB)...", round(path.stat().st_size/1e6, 1))
        count = lcount = 0
        try:
            with path.open(encoding="latin-1") as f:
                hdr = f.readline().strip().split("\t")
                ci = lambda n: hdr.index(n) if n in hdr else -1
                I = {k: ci(k) for k in ['acct','mailto','mail_addr_1','mail_city',
                     'mail_state','mail_zip','str_pfx','str_num','str','str_sfx',
                     'str_unit','site_addr_1','lgl_1','lgl_2','lgl_3','lgl_4']}
                for line in f:
                    if not line.strip(): continue
                    p = line.split("\t")
                    g = lambda k: p[I[k]].strip() if I.get(k,-1) >= 0 and I[k] < len(p) else ""
                    acct = g('acct'); owner = g('mailto')
                    site = g('site_addr_1') or " ".join(x for x in
                           [g('str_num'),g('str_pfx'),g('str'),g('str_sfx'),g('str_unit')] if x)
                    lgl = " ".join(x for x in [g('lgl_1'),g('lgl_2'),g('lgl_3'),g('lgl_4')] if x)
                    entry = {"prop_address":site,"prop_city":"Houston","prop_state":"TX",
                             "prop_zip":"","mail_address":g('mail_addr_1'),
                             "mail_city":g('mail_city'),"mail_state":g('mail_state') or "TX",
                             "mail_zip":g('mail_zip'),"hcad_acct":acct}
                    if acct: self._by_acct[acct] = entry
                    if owner:
                        for v in _variants(owner): self._by_name.setdefault(v, []).append(entry)
                    if lgl:
                        lk = _legal_key(lgl)
                        if lk and lk not in self._by_legal:
                            self._by_legal[lk] = entry; lcount += 1
                    count += 1
            log.info("real_acct: %d records | %d name | %d legal", count, len(self._by_name), lcount)
        except Exception as e: log.error("real_acct: %s", e)

    def load_deeds(self, path):
        if not path.exists(): return
        log.info("Loading deeds.txt (%s MB)...", round(path.stat().st_size/1e6, 1))
        count = 0
        try:
            with path.open(encoding="latin-1") as f:
                hdr = f.readline().strip().split("\t")
                ia = hdr.index("acct") if "acct" in hdr else 0
                ic = hdr.index("clerk_id") if "clerk_id" in hdr else 3
                for line in f:
                    if not line.strip(): continue
                    p = line.split("\t")
                    if len(p) <= max(ia, ic): continue
                    acct = p[ia].strip(); cid = p[ic].strip()
                    if acct and cid:
                        for v in _clerk_variants(cid): self._by_clerk[v] = acct
                        count += 1
            log.info("deeds: %d records | %d clerk_id keys", count, len(self._by_clerk))
        except Exception as e: log.error("deeds: %s", e)

    def lookup(self, doc_num, legal, owner, grantee, cat):
        for v in _clerk_variants(doc_num or ""):
            acct = self._by_clerk.get(v)
            if acct:
                entry = self._by_acct.get(acct)
                if entry: return entry, "high"
        lk = _legal_key(legal or "")
        if lk:
            entry = self._by_legal.get(lk)
            if entry: return entry, "high"
        if cat in ("LP","NOFC","JUD","LIEN") and grantee and _is_person(grantee):
            for v in _variants(grantee):
                hits = self._by_name.get(v)
                if hits: return hits[0], "low"
        if owner and _is_person(owner):
            for v in _variants(owner):
                hits = self._by_name.get(v)
                if hits: return hits[0], "low"
        return None, "none"

    def hcad_url(self, owner="", acct=""):
        if acct:
            return f"https://public.hcad.org/records/details.asp?crypt=&acct={acct}&taxyear=2025&type=real"
        if owner:
            enc = re.sub(r'\s+', '+', owner.strip()[:40])
            return f"https://public.hcad.org/records/Real.asp?taxyear=2025&ownername={enc}&county=harris"
        return ""

def flags_for(r):
    f = []; cat = r.get("cat",""); dt = r.get("doc_type","").upper()
    owner = r.get("owner",""); filed = r.get("filed","")
    if cat == "LP": f.append("Lis pendens")
    if cat == "NOFC": f.append("Pre-foreclosure")
    if cat == "JUD": f.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED","TAXDEED") or "TAX" in dt: f.append("Tax lien")
    if "MECH" in dt: f.append("Mechanic lien")
    if cat == "PRO": f.append("Probate / estate")
    if re.search(r"\b(LLC|CORP|INC|LTD|LP|GP|TRUST|ASSOC)\b", owner.upper()): f.append("LLC / corp owner")
    try:
        if datetime.strptime(filed[:10], "%Y-%m-%d") >= datetime.now() - timedelta(days=LOOKBACK_DAYS):
            f.append("New this week")
    except: pass
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

def parse_amt(t):
    try: return float(re.sub(r"[^\d.]", "", str(t).replace(",", "")))
    except: return 0.0

def parse_date(t):
    for fmt in ("%m/%d/%Y","%Y-%m-%d","%m-%d-%Y","%B %d, %Y","%b %d, %Y"):
        try: return datetime.strptime(str(t).strip()[:20], fmt).strftime("%Y-%m-%d")
        except: pass
    return str(t)[:10]

def parse_row_8cell(cells):
    if len(cells) != 8: return None
    def ct(i): return cells[i].get_text(" ", strip=True) if i < len(cells) else ""
    raw_file = ct(1).strip()
    filed    = ct(2).strip()
    raw_type = ""
    a = cells[3].find("a") if len(cells) > 3 else None
    if a: raw_type = a.get_text(strip=True)
    if not raw_type:
        for tok in ct(3).split():
            if tok and not tok.isdigit(): raw_type = tok; break
    raw_names = ct(4)
    raw_legal = ct(5)
    cat_result = categorize(raw_type)
    if not cat_result: return None
    cat, cat_label = cat_result
    doc_num = ""
    if re.match(r'^RP-\d{4}-\d+$', raw_file): doc_num = raw_file
    if not doc_num:
        for cell in cells:
            txt = cell.get_text(strip=True)
            if re.match(r'^RP-\d{4}-\d+$', txt): doc_num = txt; break
    clerk_url = "https://www.cclerk.hctx.net/Applications/WebSearch/RP.aspx"
    grantor, grantee = _extract_names(raw_names)
    return {"doc_num":doc_num,"doc_type":raw_type,"filed":parse_date(filed),
            "cat":cat,"cat_label":cat_label,"owner":grantor,"grantee":grantee,
            "amount":0.0,"legal":raw_legal,
            "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
            "mail_address":"","mail_city":"","mail_state":"","mail_zip":"",
            "clerk_url":clerk_url,"match_confidence":"none","hcad_url":"",
            "flags":[],"score":0}

def parse_html(html, source_url=""):
    records = []
    soup = BeautifulSoup(html, "lxml")
    all_tables = soup.find_all("table")
    best_table = None; best_count = 0
    for t in all_tables:
        c = sum(1 for tr in t.find_all("tr")
                if len(tr.find_all("td", recursive=False)) == 8)
        if c > best_count: best_count, best_table = c, t
    if not best_table or best_count == 0:
        all_trs = [tr for tr in soup.find_all("tr")
                   if len(tr.find_all("td", recursive=False)) == 8]
        for tr in all_trs:
            try:
                rec = parse_row_8cell(tr.find_all("td", recursive=False))
                if rec: records.append(rec)
            except: pass
        return records
    log.info("  Table: %d 8-cell rows", best_count)
    for tr in best_table.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        try:
            rec = parse_row_8cell(cells)
            if rec: records.append(rec)
        except Exception as e:
            log.debug("Row error: %s", e)
    if _UNKNOWN_TYPES:
        top = sorted(_UNKNOWN_TYPES.items(), key=lambda x: -x[1])[:10]
        log.info("Unknown types: %s", ", ".join(f"{k}:{v}" for k, v in top))
    return records

async def scrape_with_playwright(start_date, end_date):
    if not HAS_PLAYWRIGHT: return []
    records = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900})
        ctx.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        page = await ctx.new_page()
        log.info("Loading clerk portal ...")
        await page.goto(CLERK_HOME, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)
        await page.goto(CLERK_BASE, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await asyncio.sleep(2)
        filled = await page.evaluate(
            f"() => {{"
            f"  function s(id,v){{var e=document.getElementById(id);"
            f"    if(!e) return false; e.value=v;"
            f"    ['input','change','blur'].forEach(function(ev){{"
            f"      e.dispatchEvent(new Event(ev,{{bubbles:true}}));}});"
            f"    return true;}}"
            f"  return [s('ctl00_ContentPlaceHolder1_txtFrom','{start_date}'),"
            f"          s('ctl00_ContentPlaceHolder1_txtTo','{end_date}')];"
            f"}}"
        )
        log.info("Fill result: %s", filled)
        try:
            await page.locator("#ctl00_ContentPlaceHolder1_txtFrom").fill(start_date)
            await page.locator("#ctl00_ContentPlaceHolder1_txtTo").fill(end_date)
        except: pass
        try:
            df = await page.locator("#ctl00_ContentPlaceHolder1_txtFrom").input_value()
            dt = await page.locator("#ctl00_ContentPlaceHolder1_txtTo").input_value()
            log.info("Dates set: from=%s to=%s", df, dt)
        except: pass
        await page.locator("#ctl00_ContentPlaceHolder1_btnSearch").click()
        log.info("Search submitted")
        for wait_s in [3, 5, 8, 10]:
            await asyncio.sleep(wait_s)
            row_count = await page.evaluate(
                "() => document.querySelectorAll('table tr').length")
            log.info("  After %ds: %d table rows", wait_s, row_count)
            if row_count > 10: break
        html = await page.evaluate("() => document.documentElement.outerHTML")
        log.info("  HTML length: %d chars", len(html))
        page_records = parse_html(html, page.url)
        records.extend(page_records)
        log.info("Page 1: %d records", len(page_records))
        page_num = 1
        while page_num < 200 and page_records:
            page_num += 1
            clicked = False
            for sel in ["a:has-text('Next')","a:has-text('>')","input[value='Next']","a[id*='Next']"]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click(timeout=3000)
                        clicked = True; break
                except: pass
            if not clicked: break
            await asyncio.sleep(2)
            html = await page.evaluate("() => document.documentElement.outerHTML")
            page_records = parse_html(html)
            if not page_records: break
            records.extend(page_records)
            log.info("Page %d: %d records (total: %d)", page_num, len(page_records), len(records))
        await browser.close()
    log.info("Playwright scrape: %d records", len(records))
    return records

def scrape_foreclosures(session, cutoff):
    records = []
    try:
        r = session.get(CLERK_FRCL, timeout=30)
        soup = BeautifulSoup(r.text, "lxml")
        tables = soup.find_all("table")
        table = max(tables, key=lambda t: len(t.find_all("tr"))) if tables else None
        if not table: return records
        rows = table.find_all("tr")
        for tr in rows[1:]:
            cells = tr.find_all("td")
            if not cells: continue
            texts = [c.get_text(strip=True) for c in cells]
            doc_num = next((t for t in texts if re.match(r'^RP-\d{4}-\d+$', t)), "")
            filed = next((t for t in texts if re.match(r'^\d{2}/\d{2}/\d{4}$', t)), "")
            if filed and parse_date(filed) < cutoff: continue
            grantor = texts[3] if len(texts) > 3 else ""
            records.append({"doc_num":doc_num,"doc_type":"NOFC","filed":parse_date(filed),
                "cat":"NOFC","cat_label":"Notice of Foreclosure","owner":grantor,
                "grantee":"","amount":0.0,"legal":"",
                "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
                "mail_address":"","mail_city":"","mail_state":"","mail_zip":"",
                "clerk_url":CLERK_BASE,"match_confidence":"none","hcad_url":"","flags":[],"score":0})
        log.info("Foreclosures: %d", len(records))
    except Exception as e: log.warning("FRCL: %s", e)
    return records

def _cache_fresh(path):
    return path.exists() and (time.time() - path.stat().st_mtime) < (CACHE_MAX_DAYS * 86400)

def download_gdrive(session, file_id, dest):
    if not file_id: return False
    log.info("Downloading GDrive %s ...", file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = session.get(f"https://drive.google.com/uc?export=download&id={file_id}",
                        timeout=60, allow_redirects=True)
        if b"confirm" in r.content[:3000] or "text/html" in r.headers.get("content-type",""):
            token = re.search(rb'confirm=([^&"\']+)', r.content)
            uuid  = re.search(rb'uuid=([^&"\']+)', r.content)
            if token:
                r = session.get(f"https://drive.google.com/uc?export=download&confirm={token.group(1).decode()}&id={file_id}", timeout=600, stream=True)
            elif uuid:
                r = session.get(f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t&uuid={uuid.group(1).decode()}", timeout=600, stream=True)
            else:
                r = session.get(f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t", timeout=600, stream=True)
        if "text/html" in r.headers.get("content-type",""): return False
        if r.status_code == 200:
            written = 0
            with dest.open("wb") as f:
                for chunk in r.iter_content(1024*1024):
                    if chunk: f.write(chunk); written += len(chunk)
            mb = round(written/1e6, 1)
            log.info("  Saved %s MB -> %s", mb, dest)
            return mb > 0.5
    except Exception as e: log.error("GDrive: %s", e)
    return False

def load_parcel_db(session):
    db = ParcelDB()
    for uid, cache, loader in [
        (GDRIVE_REAL_ACCT_ID, REAL_ACCT_CACHE, db.load_real_acct),
        (GDRIVE_DEEDS_ID, DEEDS_CACHE, db.load_deeds),
    ]:
        if _cache_fresh(cache): log.info("Using cached %s", cache.name)
        elif uid: download_gdrive(session, uid, cache)
        if cache.exists(): loader(cache)
    return db

def enrich(records, db):
    counts = {"high":0,"low":0,"none":0}
    for r in records:
        hit, conf = db.lookup(r.get("doc_num",""), r.get("legal",""),
                              r.get("owner",""), r.get("grantee",""), r.get("cat",""))
        if hit:
            for k, v in hit.items():
                if v and k != "hcad_acct": r[k] = v
            r["match_confidence"] = conf
            r["hcad_url"] = db.hcad_url(r.get("owner",""), hit.get("hcad_acct",""))
        else:
            r["match_confidence"] = "none"
            r["hcad_url"] = db.hcad_url(r.get("owner",""))
        counts[conf if conf in counts else "none"] += 1
    log.info("Enrichment: high=%d low=%d none=%d", counts["high"], counts["low"], counts["none"])
    return records

def dedupe(records):
    seen, out = set(), []
    for r in records:
        k = r.get("doc_num") or f"{r.get('owner')}|{r.get('filed')}"
        if k and k not in seen: seen.add(k); out.append(r)
    return out

def apply_scores(records):
    for r in records:
        f = flags_for(r); r["flags"] = f; r["score"] = score_for(r, f)
    return records

def save_json(records, s, e):
    payload = {"fetched_at":datetime.now(timezone.utc).isoformat(),
               "source":"Harris County Clerk - Real Property Records",
               "date_range":{"from":s,"to":e},"total":len(records),
               "with_address":sum(1 for r in records if r.get("prop_address") or r.get("mail_address")),
               "records":records}
    for p in OUTPUT_PATHS:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        log.info("Saved %d records -> %s", len(records), p)

def save_csv(records):
    GHL_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GHL_CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=GHL_COLUMNS); w.writeheader()
        for r in records:
            pts = (r.get("owner") or "").strip().split()
            fn = pts[0] if pts else ""; ln = " ".join(pts[1:]) if len(pts) > 1 else ""
            w.writerow({"First Name":fn,"Last Name":ln,
                "Mailing Address":r.get("mail_address",""),"Mailing City":r.get("mail_city",""),
                "Mailing State":r.get("mail_state",""),"Mailing Zip":r.get("mail_zip",""),
                "Property Address":r.get("prop_address",""),"Property City":r.get("prop_city",""),
                "Property State":r.get("prop_state","TX"),"Property Zip":r.get("prop_zip",""),
                "Lead Type":r.get("cat_label",""),"Document Type":r.get("doc_type",""),
                "Date Filed":r.get("filed",""),"Document Number":r.get("doc_num",""),
                "Amount/Debt Owed":r.get("amount",""),"Seller Score":r.get("score",0),
                "Motivated Seller Flags":"; ".join(r.get("flags",[])),"Source":"Harris County Clerk",
                "Public Records URL":r.get("clerk_url",""),
                "Match Confidence":r.get("match_confidence",""),
                "HCAD Lookup URL":r.get("hcad_url","")})
    log.info("GHL CSV -> %s", GHL_CSV_PATH)

async def main():
    end_dt = datetime.now(); start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    start = start_dt.strftime("%m/%d/%Y"); end = end_dt.strftime("%m/%d/%Y")
    iso_s = start_dt.strftime("%Y-%m-%d"); iso_e = end_dt.strftime("%Y-%m-%d")
    log.info("="*60)
    log.info("Harris County Lead Scraper -- %s to %s", start, end)
    log.info("="*60)
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
    parcel_db = load_parcel_db(session)
    records = await scrape_with_playwright(start, end)
    records.extend(scrape_foreclosures(session, iso_s))
    log.info("Raw: %d", len(records))
    records = dedupe(records)
    records = enrich(records, parcel_db)
    records = apply_scores(records)
    records.sort(key=lambda r: r.get("score", 0), reverse=True)
    save_json(records, iso_s, iso_e)
    save_csv(records)
    log.info("="*60)
    log.info("Done. %d leads.", len(records))
    log.info("="*60)

if __name__ == "__main__":
    asyncio.run(main())
