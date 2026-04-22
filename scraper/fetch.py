"""
Harris County Motivated Seller Lead Scraper
- Searches clerk portal by DATE RANGE ONLY (no instrument type filter)
- Filters results by doc type from the returned data
- Parses legal descriptions from results for deterministic address matching
- Enriches via deeds.txt -> real_acct.txt lookup chain
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

# Doc types we want to keep — everything else is discarded
TARGET_TYPES = {
    "LP","NOFC","TAXDEED","JUD","CCJ","DRJUD",
    "LNCORPTX","LNIRS","LNFED","LN","LNMECH","LNHOA","MEDLN",
    "PRO","NOC","RELLP",
    # Common aliases seen in portal results
    "LIS PENDENS","NOTICE OF FORECLOSURE","TAX DEED",
    "JUDGMENT","LIEN","MECHANIC LIEN","HOA LIEN","MEDICAID LIEN",
    "PROBATE","NOTICE OF COMMENCEMENT","RELEASE LIS PENDENS",
    # Partial matches handled in categorize()
}

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

# Track unknown types for debugging
_UNKNOWN_TYPES: dict[str,int] = {}

def categorize(doc_type: str) -> tuple[str,str] | None:
    # Map a raw doc type string to (cat, cat_label). Returns None if not a target type.
    dt = doc_type.upper().strip()

    # Direct match
    if dt in DOC_TYPE_MAP:
        return DOC_TYPE_MAP[dt]

    # Partial keyword match — ordered by specificity
    kw = [
        ("LIS PEND", ("LP","Lis Pendens")),
        ("FORECLOS", ("NOFC","Notice of Foreclosure")),
        ("TAX DEED", ("TAXDEED","Tax Deed")),
        ("TAXDEED",  ("TAXDEED","Tax Deed")),
        ("DRJUD",    ("JUD","Domestic Judgment")),
        ("CCJ",      ("JUD","Certified Judgment")),
        ("JUDGMENT", ("JUD","Judgment")),
        ("JUD",      ("JUD","Judgment")),
        ("IRS LN",   ("LIEN","IRS Lien")),
        ("LNIRS",    ("LIEN","IRS Lien")),
        ("FED LN",   ("LIEN","Federal Lien")),
        ("LNFED",    ("LIEN","Federal Lien")),
        ("CORP TAX", ("LIEN","Corp Tax Lien")),
        ("LNCORPTX", ("LIEN","Corp Tax Lien")),
        ("MECH",     ("LIEN","Mechanic Lien")),
        ("LNMECH",   ("LIEN","Mechanic Lien")),
        ("HOA LN",   ("LIEN","HOA Lien")),
        ("LNHOA",    ("LIEN","HOA Lien")),
        ("MEDICAID", ("LIEN","Medicaid Lien")),
        ("MEDLN",    ("LIEN","Medicaid Lien")),
        ("COMMENCE", ("NOC","Notice of Commencement")),
        ("NOC",      ("NOC","Notice of Commencement")),
        ("RELLP",    ("RELLP","Release Lis Pendens")),
        ("REL LP",   ("RELLP","Release Lis Pendens")),
        ("PROBATE",  ("PRO","Probate Document")),
        ("PRO",      ("PRO","Probate Document")),
        # Common lien types from portal
        ("LN ",      ("LIEN","Lien")),
        (" LN",      ("LIEN","Lien")),
        ("LIEN",     ("LIEN","Lien")),
        # Abstract of judgment
        ("ABST",     ("JUD","Judgment")),
        ("AJ",       ("JUD","Judgment")),
        # Tax lien variants
        ("TAX LN",   ("LIEN","Tax Lien")),
        ("FED TAX",  ("LIEN","Federal Tax Lien")),
        ("ST TAX",   ("LIEN","State Tax Lien")),
        ("CITY TAX", ("LIEN","City Tax Lien")),
        # Lis pendens variants
        ("LIS P",    ("LP","Lis Pendens")),
        ("LP",       ("LP","Lis Pendens")),
        ("L/P",      ("LP","Lis Pendens")),
        # Abstract of judgment
        ("A/J",      ("JUD","Abstract of Judgment")),
        ("ABST JUD", ("JUD","Abstract of Judgment")),
        # Financing statement = UCC/commercial lien
        ("FI STM",   ("LIEN","Financing Statement")),
        ("FIN STM",  ("LIEN","Financing Statement")),
        # Notice types
        ("NOTICE OF FORECL", ("NOFC","Notice of Foreclosure")),
        ("NOTICE OF COMMENCE",("NOC","Notice of Commencement")),
        ("NOTICE OF LIS",    ("LP","Lis Pendens")),
    ]
    for key, val in kw:
        if key in dt:
            return val

    # Track for debugging
    _UNKNOWN_TYPES[dt] = _UNKNOWN_TYPES.get(dt, 0) + 1
    return None

# ── Clerk ID normalization ────────────────────────────────────────────────────

def _clerk_variants(raw: str) -> list[str]:
    s = raw.strip().upper().replace(" ","")
    v = {s}
    m = re.match(r'^RP-(\d{4})-(\d+)$', s)
    if m:
        v.add(f"{m.group(1)}-RP-{m.group(2)}")
        v.add(m.group(2).lstrip('0') or '0')
    m2 = re.match(r'^(\d{4})-RP-(\d+)$', s)
    if m2:
        v.add(f"RP-{m2.group(1)}-{m2.group(2)}")
        v.add(m2.group(2).lstrip('0') or '0')
    if s.isdigit():
        v.add(s.lstrip('0') or '0')
    return list(v)

# ── Name normalization ────────────────────────────────────────────────────────

def _norm(s): return re.sub(r"\s+", " ", str(s).strip().upper())

def _variants(name):
    n = _norm(name); p = n.split(); v = [n]
    if len(p) >= 2:
        v += [f"{p[-1]} {' '.join(p[:-1])}", f"{p[-1]}, {' '.join(p[:-1])}"]
    if len(p) >= 3: v.append(f"{p[0]} {p[1]}")
    clean = re.sub(r"\b(LLC|INC|CORP|LTD|LP|GP|TRUST|ESTATE|ET AL|JR|SR|II|III)\b","",n).strip()
    if clean and clean != n:
        cp = clean.split(); v.append(clean)
        if len(cp) >= 2:
            v += [f"{cp[-1]} {' '.join(cp[:-1])}"]
    return list(dict.fromkeys(x for x in v if x))

# ── Legal description parsing ─────────────────────────────────────────────────

def _parse_legal(legal: str) -> dict:
    if not legal: return {}
    s = legal.upper()
    # Parse Desc:SUBDIV Sec:N Lot:N Block:N format from clerk results
    result = {}
    m = re.search(r'DESC[:\s]+([^,\n]+?)(?=\s*SEC[:\s]|\s*LOT[:\s]|\s*BLK[:\s]|$)', s)
    if m: result['sub'] = m.group(1).strip()[:60]
    m = re.search(r'LOT[:\s]+(\w+)', s)
    if m: result['lot'] = m.group(1)
    m = re.search(r'(?:BLK|BLOCK)[:\s]+(\w+)', s)
    if m: result['block'] = m.group(1)
    m = re.search(r'SEC[:\s]+(\w+)', s)
    if m: result['sec'] = m.group(1)
    # Also parse traditional format: LT 5 BLK 12 SUBDIVISION NAME
    if not result.get('lot'):
        m = re.search(r'\bLT\s+(\w+)', s)
        if m: result['lot'] = m.group(1)
    if not result.get('block'):
        m = re.search(r'\bBLK\s+(\w+)', s)
        if m: result['block'] = m.group(1)
    if not result.get('sub'):
        sub = re.sub(r'\b(LT|BLK|SEC|TR|ABST|UNIT|PHASE|LOT|BLOCK)\s*[\dA-Z]+','',s)
        sub = re.sub(r'\s+',' ',sub).strip(' &,.-')
        if len(sub) > 4: result['sub'] = sub[:60]
    return result

def _legal_key(legal: str) -> str | None:
    p = _parse_legal(legal)
    if p.get('lot') and p.get('block') and p.get('sub'):
        return f"{p['sub'][:40]}|{p['block']}|{p['lot']}"
    if p.get('lot') and p.get('sub'):
        return f"{p['sub'][:40]}|{p['lot']}"
    return None

# ── Parcel DB ─────────────────────────────────────────────────────────────────

class ParcelDB:
    def __init__(self):
        self._by_name  = {}
        self._by_legal = {}
        self._by_acct  = {}
        self._by_clerk = {}
        self.loaded    = False

    def load_real_acct(self, path: Path) -> None:
        if not path.exists(): log.warning("real_acct.txt missing"); return
        log.info("Loading real_acct.txt (%s MB)...",
                 round(path.stat().st_size/1e6,1))
        count = lcount = 0
        try:
            with path.open(encoding="latin-1") as f:
                hdr = f.readline().strip().split("\t")
                ci  = lambda n: hdr.index(n) if n in hdr else -1
                I   = {k: ci(k) for k in [
                    'acct','mailto','mail_addr_1','mail_city','mail_state','mail_zip',
                    'str_pfx','str_num','str','str_sfx','str_unit','site_addr_1',
                    'lgl_1','lgl_2','lgl_3','lgl_4']}
                log.info("  Col indices: acct=%d name=%d site=%d lgl1=%d",
                         I['acct'],I['mailto'],I['site_addr_1'],I['lgl_1'])
                for line in f:
                    if not line.strip(): continue
                    p = line.split("\t")
                    g = lambda k: p[I[k]].strip() if I.get(k,-1)>=0 and I[k]<len(p) else ""
                    acct  = g('acct'); owner = g('mailto')
                    site  = g('site_addr_1')
                    if not site:
                        site = " ".join(x for x in [g('str_num'),g('str_pfx'),
                                                     g('str'),g('str_sfx'),g('str_unit')] if x)
                    legal_full = " ".join(x for x in [g('lgl_1'),g('lgl_2'),
                                                       g('lgl_3'),g('lgl_4')] if x)
                    entry = {"prop_address":site,"prop_city":"Houston","prop_state":"TX",
                             "prop_zip":"","mail_address":g('mail_addr_1'),
                             "mail_city":g('mail_city'),"mail_state":g('mail_state') or "TX",
                             "mail_zip":g('mail_zip'),"hcad_acct":acct}
                    if acct: self._by_acct[acct] = entry
                    if owner:
                        for v in _variants(owner):
                            self._by_name.setdefault(v,[]).append(entry)
                    if legal_full:
                        lk = _legal_key(legal_full)
                        if lk and lk not in self._by_legal:
                            self._by_legal[lk] = entry; lcount += 1
                    count += 1
            self.loaded = True
            log.info("real_acct: %d records | %d name keys | %d legal keys",
                     count, len(self._by_name), lcount)
        except Exception as e:
            log.error("real_acct load error: %s", e)

    def load_deeds(self, path: Path) -> None:
        if not path.exists(): log.warning("deeds.txt missing"); return
        log.info("Loading deeds.txt (%s MB)...", round(path.stat().st_size/1e6,1))
        count = 0
        try:
            with path.open(encoding="latin-1") as f:
                hdr = f.readline().strip().split("\t")
                ia  = hdr.index("acct")     if "acct"     in hdr else 0
                ic  = hdr.index("clerk_id") if "clerk_id" in hdr else 3
                for line in f:
                    if not line.strip(): continue
                    p = line.split("\t")
                    if len(p) <= max(ia,ic): continue
                    acct = p[ia].strip(); cid = p[ic].strip()
                    if acct and cid:
                        for v in _clerk_variants(cid):
                            self._by_clerk[v] = acct
                        count += 1
            log.info("deeds: %d records | %d clerk_id keys", count, len(self._by_clerk))
        except Exception as e:
            log.error("deeds load error: %s", e)

    def lookup(self, doc_num, legal, owner, grantee, cat):
        # Layer 0: doc number -> deeds -> acct -> address
        for v in _clerk_variants(doc_num or ""):
            acct = self._by_clerk.get(v)
            if acct:
                entry = self._by_acct.get(acct)
                if entry: return entry, "high"

        # Layer 1: legal description
        lk = _legal_key(legal or "")
        if lk:
            entry = self._by_legal.get(lk)
            if entry: return entry, "high"

        # Layer 2: grantee for distress docs (actual property owner)
        if cat in ("LP","NOFC","JUD","LIEN") and grantee:
            for v in _variants(grantee):
                hits = self._by_name.get(v)
                if hits: return hits[0], "low"

        # Layer 3: grantor — only individual persons
        if owner and len(owner.split()) >= 2:
            skip = {"BANK","MORTGAGE","TRUST","CORP","LLC","INC","SYSTEMS",
                    "ELECTRONIC","REGISTRATION","NATIONAL","FEDERAL","AMERICA",
                    "FINANCE","CAPITAL","INVESTMENT","SERVICES","ASSOCIATION"}
            if not any(s in owner.upper() for s in skip):
                for v in _variants(owner):
                    hits = self._by_name.get(v)
                    if hits: return hits[0], "low"

        return None, "none"

    def hcad_url(self, owner="", acct=""):
        if acct:
            return f"https://public.hcad.org/records/details.asp?crypt=&acct={acct}&taxyear=2025&type=real"
        if owner:
            enc = re.sub(r'\s+','+',owner.strip()[:40])
            return f"https://public.hcad.org/records/Real.asp?taxyear=2025&ownername={enc}&county=harris"
        return ""

# ── Scoring ──────────────────────────────────────────────────────────────────

def flags_for(r):
    f=[]; cat=r.get("cat",""); dt=r.get("doc_type","").upper()
    owner=r.get("owner",""); filed=r.get("filed","")
    if cat=="LP":    f.append("Lis pendens")
    if cat=="NOFC":  f.append("Pre-foreclosure")
    if cat=="JUD":   f.append("Judgment lien")
    if dt in ("LNCORPTX","LNIRS","LNFED","TAXDEED") or "TAX" in dt: f.append("Tax lien")
    if "MECH" in dt or dt=="LNMECH": f.append("Mechanic lien")
    if cat=="PRO":   f.append("Probate / estate")
    if re.search(r"\b(LLC|CORP|INC|LTD|LP|GP|TRUST|ASSOC)\b",owner.upper()): f.append("LLC / corp owner")
    try:
        if datetime.strptime(filed[:10],"%Y-%m-%d")>=datetime.now()-timedelta(days=LOOKBACK_DAYS):
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

CORP_SKIP = {"BANK","TRUST","CORP","LLC","INC","MORTGAGE","ELECTRONIC",
             "REGISTRATION","SYSTEMS","SERIES","NATIONAL","FEDERAL","AMERICA",
             "FINANCE","CAPITAL","INVESTMENT","SERVICES","FUNDING","ASSOCIATION"}

def _is_person(name):
    return not any(s in name.upper() for s in CORP_SKIP) and len(name.split())>=2

def _extract_names(raw):
    if not raw: return "",""
    if "Grantor:" not in raw and "Grantee:" not in raw:
        return raw.strip(),""
    grantors=re.findall(r"Grantor:([^G]+?)(?=Grantor:|Grantee:|$)",raw)
    grantees=re.findall(r"Grantee:([^G]+?)(?=Grantor:|Grantee:|$)",raw)
    grantors=[n.strip() for n in grantors if n.strip()]
    grantees=[n.strip() for n in grantees if n.strip()]
    bg = next((n for n in grantors if _is_person(n)), grantors[0] if grantors else "")
    be = next((n for n in grantees if _is_person(n)), grantees[0] if grantees else "")
    return bg, be

def _parse_names_cell(raw):
    # Portal puts Names like: "Grantor: SMITH JOHN\nGrantee: BANK OF AMERICA"
    # or just "SMITH JOHN" with separate grantee column
    grantor = grantee = ""
    if "Grantor:" in raw or "Grantee:" in raw:
        grantor, grantee = _extract_names(raw)
    elif "\n" in raw:
        parts = [p.strip() for p in raw.split("\n") if p.strip()]
        grantor = parts[0] if parts else ""
        grantee = parts[1] if len(parts)>1 else ""
    else:
        grantor = raw.strip()
    return grantor, grantee

def _extract_legal_from_cell(raw):
    # Portal legal cell format: "Desc:HAPPY HIDE A WAY\nSec: 3\nLot: 434\nComment:..."
    if not raw: return ""
    # Remove comment lines
    lines = [l for l in raw.split("\n") if not l.strip().startswith("Comment:")]
    return " ".join(l.strip() for l in lines if l.strip())

def parse_row(cells, headers):
    # Results table columns (confirmed from portal):
    # File Number | File Date | Type | Names | Legal Description | Pgs | Film Code
    def col_exact(*names):
        for nm in names:
            for i,h in enumerate(headers):
                if h==nm and i<len(cells):
                    v=cells[i].get_text(" ",strip=True)
                    if v: return v
        return ""
    def col(*names):
        for nm in sorted(names,key=len,reverse=True):
            for i,h in enumerate(headers):
                if nm in h and i<len(cells):
                    v=cells[i].get_text(" ",strip=True)
                    if v: return v
        return ""

    # CONFIRMED TABLE HEADERS:
    # ['', 'FILE NUMBER', 'FILE DATE', 'TYPEVOL PAGE', 'NAMES', 'LEGAL DESCRIPTION', 'PGS', 'FILM CODE']
    # Indices:  0     1              2             3              4       5                   6    7

    # Use positional access since we know exact column order
    def pos(i):
        return cells[i].get_text(" ", strip=True) if i < len(cells) else ""

    # Col 1: FILE NUMBER
    raw_file = pos(1)
    # Col 2: FILE DATE
    filed    = pos(2)
    # Col 3: TYPEVOL PAGE — contains "LP 1000 300" or just "LP" — extract type only
    raw_typecol = pos(3)
    # Type is the first token before any digits
    raw_type = re.match(r'^([A-Z/][A-Z0-9/ ]*?)(?:\s+\d|\s*$)', raw_typecol)
    raw_type = raw_type.group(1).strip() if raw_type else raw_typecol.split()[0] if raw_typecol.split() else ""
    # Col 4: NAMES
    raw_names = pos(4)
    # Col 5: LEGAL DESCRIPTION
    raw_legal = pos(5)
    # Col 7: FILM CODE (has the document link)
    film_col  = pos(7)

    # Get doc number and URL
    doc_num = ""; clerk_url = ""

    # Check FILE NUMBER column first
    if re.match(r'^RP-\d{4}-\d+$', raw_file) or re.match(r'^\d{4}-RP-\d+$', raw_file):
        doc_num = raw_file

    # Check FILM CODE column for link
    film_cell = cells[7] if len(cells) > 7 else None
    if film_cell:
        a = film_cell.find("a", href=True)
        if a:
            h = a["href"]
            clerk_url = h if h.startswith("http") else "https://www.cclerk.hctx.net" + h
            # Extract doc num from URL if not found yet
            if not doc_num:
                m = re.search(r'FileID=(RP-[\d-]+|\d{4}-RP-[\d]+)', h)
                if m: doc_num = m.group(1)

    # Also check FILE NUMBER cell for link
    if not clerk_url:
        file_cell = cells[1] if len(cells) > 1 else None
        if file_cell:
            a = file_cell.find("a", href=True)
            if a:
                h = a["href"]
                clerk_url = h if h.startswith("http") else "https://www.cclerk.hctx.net" + h

    if not doc_num:
        # Scan all cells for RP pattern
        for cell in cells:
            txt = cell.get_text(strip=True)
            if re.match(r'^RP-\d{4}-\d+$', txt):
                doc_num = txt; break

    if not clerk_url and doc_num:
        clerk_url = f"{CLERK_BASE}?FileID={doc_num}"

    amt_s = ""  # Amount not in standard results table

    # Parse names
    grantor, grantee = _parse_names_cell(raw_names)

    # Parse legal
    legal = _extract_legal_from_cell(raw_legal)

    # Categorize doc type
    cat_result = categorize(raw_type)
    if not cat_result:
        return None  # Not a target doc type — skip
    cat, cat_label = cat_result

    if not doc_num and not grantor: return None

    return {"doc_num":doc_num,"doc_type":raw_type,"filed":parse_date(filed),
            "cat":cat,"cat_label":cat_label,"owner":grantor,"grantee":grantee,
            "amount":parse_amt(amt_s),"legal":legal,
            "prop_address":"","prop_city":"","prop_state":"TX","prop_zip":"",
            "mail_address":"","mail_city":"","mail_state":"","mail_zip":"",
            "clerk_url":clerk_url,"match_confidence":"none","hcad_url":"",
            "flags":[],"score":0}

# ── Playwright ────────────────────────────────────────────────────────────────

async def search_date_range(page, start_date, end_date):
    records = []
    log.info("Searching date range %s → %s (all types) ...", start_date, end_date)
    try:
        await page.goto(CLERK_BASE, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await asyncio.sleep(1)
    except Exception as e:
        log.error("Nav error: %s", e); return records

    # Fill ONLY date fields — leave instrument type blank to get all results
    try:
        # Use JavaScript to set date values directly — most reliable method
        result = await page.evaluate(f"""() => {{
            function setVal(id, val) {{
                const el = document.getElementById(id);
                if (!el) return 'NOT_FOUND:' + id;
                // Clear existing value
                el.value = '';
                // Set new value
                el.value = val;
                // Fire all necessary events for ASP.NET WebForms
                ['input','change','blur'].forEach(ev =>
                    el.dispatchEvent(new Event(ev, {{bubbles:true}})));
                return 'OK:' + id + '=' + el.value;
            }}
            return [
                setVal('ctl00_ContentPlaceHolder1_txtFrom', '{start_date}'),
                setVal('ctl00_ContentPlaceHolder1_txtTo',   '{end_date}'),
            ].join(' | ');
        }}""")
        log.info("  Date fill result: %s", result)

        # Also use Playwright fill as backup
        df = page.locator("#ctl00_ContentPlaceHolder1_txtFrom")
        dt = page.locator("#ctl00_ContentPlaceHolder1_txtTo")
        if await df.count() > 0:
            await df.click()
            await df.fill(start_date)
            await page.keyboard.press("Tab")
        if await dt.count() > 0:
            await dt.click()
            await dt.fill(end_date)
            await page.keyboard.press("Tab")
        await asyncio.sleep(0.5)

        # Verify values were set
        df_val = await page.locator("#ctl00_ContentPlaceHolder1_txtFrom").input_value()
        dt_val = await page.locator("#ctl00_ContentPlaceHolder1_txtTo").input_value()
        log.info("  Dates verified: from=%s to=%s", df_val, dt_val)
    except Exception as e:
        log.warning("  Date fill error: %s", e)

    # Click Search
    for sel in ["#ctl00_ContentPlaceHolder1_btnSearch","input[type='submit']",
                "input[value='Search']","button:has-text('Search')"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=5000)
                log.info("  Search clicked: %s", sel)
                break
        except: pass

    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except: pass
    await asyncio.sleep(2)

    # Log where we landed
    current_url = page.url
    log.info("  After search URL: %s", current_url[:80])
    title = await page.title()
    log.info("  Page title: %s", title[:60])

    # Paginate through all results
    page_num = 0
    total_rows = 0
    while page_num < 200:
        page_num += 1
        content = await page.content()
        soup    = BeautifulSoup(content, "lxml")
        txt     = soup.get_text().lower()

        if "no records found" in txt and page_num == 1:
            log.warning("  No records found for date range")
            break

        table = best_table(soup)
        if not table: break

        rows = table.find_all("tr")
        if len(rows) < 2: break
        headers = [c.get_text(strip=True).upper() for c in rows[0].find_all(["th","td"])]

        if page_num == 1:
            log.info("  Table headers: %s", headers)

        new_n = 0
        for tr in rows[1:]:
            cells = tr.find_all("td")
            if not cells: continue
            try:
                rec = parse_row(cells, headers)
                if rec:
                    records.append(rec)
                    new_n += 1
            except Exception as e:
                log.debug("  Row error: %s", e)

        total_rows += len(rows) - 1
        log.info("  Page %d: %d rows total, %d target records kept",
                 page_num, len(rows)-1, new_n)

        # Next page
        try:
            await page.click(
                "a:has-text('Next'), input[value='Next'], "
                "a[id*='Next'], input[id*='Next']",
                timeout=4000
            )
            await page.wait_for_load_state("networkidle", timeout=20_000)
            await asyncio.sleep(1)
        except:
            break

    log.info("Scraped %d total rows, kept %d target records", total_rows, len(records))
    if _UNKNOWN_TYPES:
        # Log top unknown types to help expand categorize()
        top = sorted(_UNKNOWN_TYPES.items(), key=lambda x: -x[1])[:20]
        log.info("TOP UNKNOWN DOC TYPES (not captured): %s",
                 ", ".join(f"{k}:{v}" for k,v in top))
    return records


async def scrape_foreclosures(page, cutoff):
    records = []
    log.info("Scraping foreclosure page ...")
    try:
        await page.goto(CLERK_FRCL, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await asyncio.sleep(2)
    except Exception as e:
        log.warning("  FRCL error: %s", e); return records
    content = await page.content()
    soup    = BeautifulSoup(content, "lxml")
    table   = best_table(soup)
    if not table: return records
    rows    = table.find_all("tr")
    if len(rows) < 2: return records
    headers = [c.get_text(strip=True).upper() for c in rows[0].find_all(["th","td"])]
    for tr in rows[1:]:
        cells = tr.find_all("td")
        if not cells: continue
        try:
            rec = parse_row(cells, headers)
            if rec is None:
                # Force NOFC category for foreclosure page records
                rec = parse_row.__wrapped__(cells, headers) if hasattr(parse_row,'__wrapped__') else None
            if rec and (not rec.get("filed") or rec["filed"] >= cutoff):
                rec["cat"] = "NOFC"; rec["cat_label"] = "Notice of Foreclosure"
                records.append(rec)
        except: pass
    log.info("  → %d foreclosures", len(records))
    return records


async def scrape_all(start_date, end_date):
    if not HAS_PLAYWRIGHT: log.error("No Playwright"); return []
    all_records = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":800})
        ctx.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        page = await ctx.new_page()

        # Warm up session
        log.info("Warming up session ...")
        try:
            await page.goto(CLERK_HOME, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)
            await asyncio.sleep(3)
            log.info("Session: %s", await page.title())
        except Exception as e:
            log.warning("Warmup: %s", e)

        # Single search for all records in date range
        try:
            recs = await search_date_range(page, start_date, end_date)
            all_records.extend(recs)
        except Exception as e:
            log.error("Main search error: %s", e)

        # Supplement with foreclosure page
        cutoff = (datetime.now()-timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        try:
            frcl = await scrape_foreclosures(page, cutoff)
            all_records.extend(frcl)
        except Exception as e:
            log.warning("FRCL error: %s", e)

        await browser.close()
    return all_records

# ── Google Drive download ─────────────────────────────────────────────────────

def _cache_fresh(path):
    return path.exists() and (time.time()-path.stat().st_mtime)<(CACHE_MAX_DAYS*86400)

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
                r = session.get(
                    f"https://drive.google.com/uc?export=download&confirm={token.group(1).decode()}&id={file_id}",
                    timeout=600, stream=True)
            elif uuid:
                r = session.get(
                    f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t&uuid={uuid.group(1).decode()}",
                    timeout=600, stream=True)
            else:
                r = session.get(
                    f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t",
                    timeout=600, stream=True)
        ct = r.headers.get("content-type","")
        if "text/html" in ct:
            log.error("  GDrive returned HTML — file may not be public")
            return False
        if r.status_code == 200:
            written = 0
            with dest.open("wb") as f:
                for chunk in r.iter_content(1024*1024):
                    if chunk: f.write(chunk); written += len(chunk)
            mb = round(written/1e6,1)
            log.info("  Saved %s MB → %s", mb, dest)
            return mb > 0.5
    except Exception as e:
        log.error("  Download error: %s", e)
    return False

def load_parcel_db(session):
    db = ParcelDB()
    for url_id, cache, loader in [
        (GDRIVE_REAL_ACCT_ID, REAL_ACCT_CACHE, db.load_real_acct),
        (GDRIVE_DEEDS_ID,     DEEDS_CACHE,     db.load_deeds),
    ]:
        if _cache_fresh(cache):
            log.info("Using cached %s", cache.name)
        elif url_id:
            ok = download_gdrive(session, url_id, cache)
            if not ok: log.warning("Download failed for %s", cache.name)
        if cache.exists():
            loader(cache)
    return db

# ── Pipeline ─────────────────────────────────────────────────────────────────

def enrich(records, db):
    counts = {"high":0,"low":0,"none":0}
    for r in records:
        hit, conf = db.lookup(
            doc_num = r.get("doc_num",""),
            legal   = r.get("legal",""),
            owner   = r.get("owner",""),
            grantee = r.get("grantee",""),
            cat     = r.get("cat",""),
        )
        if hit:
            for k,v in hit.items():
                if v and k != "hcad_acct": r[k] = v
            r["match_confidence"] = conf
            r["hcad_url"] = db.hcad_url(r.get("owner",""), hit.get("hcad_acct",""))
        else:
            r["match_confidence"] = "none"
            r["hcad_url"] = db.hcad_url(r.get("owner",""))
        counts[conf if conf in counts else "none"] += 1
    log.info("Enrichment: high=%d low=%d none=%d",
             counts["high"], counts["low"], counts["none"])
    return records

def dedupe(records):
    seen,out=set(),[]
    for r in records:
        k=r.get("doc_num") or f"{r.get('owner')}|{r.get('filed')}"
        if k and k not in seen: seen.add(k); out.append(r)
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
        log.info("Saved %d records → %s", len(records), p)

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
                "Public Records URL":r.get("clerk_url",""),
                "Match Confidence":r.get("match_confidence",""),
                "HCAD Lookup URL":r.get("hcad_url","")})
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
    parcel_db=load_parcel_db(session)
    records=await scrape_all(start, end)
    log.info("Raw scraped: %d", len(records))
    records=dedupe(records)
    log.info("After dedupe: %d", len(records))
    records=enrich(records, parcel_db)
    records=apply_scores(records)
    records.sort(key=lambda r: r.get("score",0), reverse=True)
    save_json(records, iso_s, iso_e)
    save_csv(records)
    log.info("="*60)
    log.info("Done. %d target leads from %s to %s", len(records), iso_s, iso_e)
    log.info("="*60)

if __name__=="__main__":
    asyncio.run(main())
