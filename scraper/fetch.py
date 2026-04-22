"""
Harris County Motivated Seller Lead Scraper
4-layer enrichment: Legal Description → Grantee → Name → Fallback
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

# ── Clerk ID normalization ───────────────────────────────────────────────────

def _clerk_id_variants(raw: str) -> list[str]:
    # Return all possible normalized forms of a clerk document ID.
    s = raw.strip().upper().replace(" ", "")
    variants = {s}
    m = re.match(r'^RP-(\d{4})-(\d+)$', s)
    if m:
        variants.add(f"{m.group(1)}-RP-{m.group(2)}")
        variants.add(f"RP-{m.group(2)}")
        variants.add(m.group(2).lstrip('0') or '0')
    m2 = re.match(r'^(\d{4})-RP-(\d+)$', s)
    if m2:
        variants.add(f"RP-{m2.group(1)}-{m2.group(2)}")
        variants.add(f"RP-{m2.group(2)}")
        variants.add(m2.group(2).lstrip('0') or '0')
    if s.isdigit():
        variants.add(s.lstrip('0') or '0')
    return list(variants)


# ── Name normalization ────────────────────────────────────────────────────────

def _norm(s): return re.sub(r"\s+", " ", str(s).strip().upper())

def _variants(name):
    n = _norm(name); p = n.split(); v = [n]
    if len(p) >= 2:
        v += [f"{p[-1]} {' '.join(p[:-1])}", f"{p[-1]}, {' '.join(p[:-1])}"]
    if len(p) >= 3:
        v.append(f"{p[0]} {p[1]}")
    clean = re.sub(r"\b(LLC|INC|CORP|LTD|LP|GP|TRUST|ESTATE|ET AL|JR|SR|II|III)\b","",n).strip()
    if clean and clean != n:
        cp = clean.split()
        v.append(clean)
        if len(cp) >= 2:
            v += [f"{cp[-1]} {' '.join(cp[:-1])}", f"{cp[-1]}, {' '.join(cp[:-1])}"]
    return list(dict.fromkeys(x for x in v if x))

# ── Legal description parsing ─────────────────────────────────────────────────

def _parse_legal(legal: str) -> dict:
    """Extract lot, block, subdivision from a legal description string."""
    if not legal:
        return {}
    s = legal.upper().strip()
    result = {}
    # Lot
    m = re.search(r'\bLT?\s*(\d+[A-Z]?)', s)
    if m: result['lot'] = m.group(1)
    # Block
    m = re.search(r'\bBLK?\s*(\d+[A-Z]?)', s)
    if m: result['block'] = m.group(1)
    # Section
    m = re.search(r'\bSEC\s*(\d+)', s)
    if m: result['sec'] = m.group(1)
    # Subdivision — everything after block/lot info
    sub = re.sub(r'\b(LT?|BLK?|SEC|TR|ABST|AB|UNIT|PHASE)\s*[\dA-Z]+', '', s)
    sub = re.sub(r'\s+', ' ', sub).strip(' &,.-')
    if len(sub) > 4:
        result['sub'] = sub[:60]
    return result

def _legal_key(legal: str) -> str | None:
    """Create a normalized lookup key from a legal description."""
    p = _parse_legal(legal)
    if p.get('lot') and p.get('block') and p.get('sub'):
        return f"{p['sub'].strip()[:40]}|{p['block']}|{p['lot']}"
    if p.get('lot') and p.get('sub'):
        return f"{p['sub'].strip()[:40]}|{p['lot']}"
    return None

# ── Parcel DB ─────────────────────────────────────────────────────────────────

class ParcelDB:
    def __init__(self):
        self._by_name  : dict[str, list[dict]] = {}
        self._by_legal : dict[str, dict]       = {}
        self._by_acct  : dict[str, dict]       = {}
        self._by_clerk : dict[str, str]        = {}  # clerk_id -> acct
        self.loaded = False

    def load_real_acct(self, path: Path) -> None:
        if not path.exists():
            log.warning("real_acct.txt not found: %s", path); return
        sz = round(path.stat().st_size / 1_000_000, 1)
        log.info("Loading real_acct.txt (%s MB) ...", sz)
        count = legal_count = 0
        try:
            with path.open(encoding="latin-1") as f:
                hdr  = f.readline().strip().split("\t")
                ci   = lambda n: hdr.index(n) if n in hdr else -1
                I = {
                    'acct':  ci("acct"),  'name':  ci("mailto"),
                    'ma1':   ci("mail_addr_1"), 'mc': ci("mail_city"),
                    'ms':    ci("mail_state"),  'mz': ci("mail_zip"),
                    'spfx':  ci("str_pfx"),  'snum': ci("str_num"),
                    'str':   ci("str"),    'ssfx':  ci("str_sfx"),
                    'sunit': ci("str_unit"), 'site': ci("site_addr_1"),
                    'lgl1':  ci("lgl_1"),  'lgl2':  ci("lgl_2"),
                    'lgl3':  ci("lgl_3"),  'lgl4':  ci("lgl_4"),
                }
                log.info("Key col indices: %s", {k:v for k,v in I.items() if v>=0})

                for line in f:
                    if not line.strip(): continue
                    p = line.split("\t")
                    g = lambda k: p[I[k]].strip() if I.get(k,-1) >= 0 and I[k] < len(p) else ""

                    acct  = g('acct')
                    owner = g('name')

                    # Build site address
                    site  = g('site')
                    if not site:
                        parts = [g('snum'), g('spfx'), g('str'), g('ssfx'), g('sunit')]
                        site  = " ".join(x for x in parts if x).strip()

                    # Build full legal description
                    legal_full = " ".join(x for x in [g('lgl1'),g('lgl2'),g('lgl3'),g('lgl4')] if x).strip()

                    entry = {
                        "prop_address": site,
                        "prop_city":    "Houston",
                        "prop_state":   "TX",
                        "prop_zip":     "",
                        "mail_address": g('ma1'),
                        "mail_city":    g('mc'),
                        "mail_state":   g('ms') or "TX",
                        "mail_zip":     g('mz'),
                        "hcad_acct":    acct,
                    }

                    if acct:
                        self._by_acct[acct] = entry

                    # Index by owner name
                    if owner:
                        for v in _variants(owner):
                            self._by_name.setdefault(v, []).append(entry)

                    # Index by legal description key
                    if legal_full:
                        lk = _legal_key(legal_full)
                        if lk and lk not in self._by_legal:
                            self._by_legal[lk] = entry
                            legal_count += 1

                    count += 1

            self.loaded = True
            log.info("ParcelDB: %d records | %d name keys | %d legal keys",
                     count, len(self._by_name), legal_count)
        except Exception as e:
            log.error("real_acct load error: %s", e)

    def load_deeds(self, path: Path) -> None:
        # Load deeds.txt: maps clerk_id -> acct for deterministic doc lookup
        if not path.exists():
            log.warning("deeds.txt not found: %s", path); return
        sz = round(path.stat().st_size / 1_000_000, 1)
        log.info("Loading deeds.txt (%s MB) ...", sz)
        count = 0
        try:
            with path.open(encoding="latin-1") as f:
                hdr = f.readline().strip().split("	")
                # Columns: acct, dos, clerk_yr, clerk_id, deed_id
                ia = hdr.index("acct")     if "acct"     in hdr else 0
                ic = hdr.index("clerk_id") if "clerk_id" in hdr else 3
                for line in f:
                    if not line.strip(): continue
                    p = line.split("	")
                    if len(p) <= max(ia, ic): continue
                    acct     = p[ia].strip()
                    clerk_id = p[ic].strip()
                    if acct and clerk_id:
                        for v in _clerk_id_variants(clerk_id):
                            self._by_clerk[v] = acct
                        count += 1
            log.info("Deeds index: %d records, %d clerk_id keys",
                     count, len(self._by_clerk))
        except Exception as e:
            log.error("deeds.txt load error: %s", e)

    # ── Lookup methods ────────────────────────────────────────────────────────

    def lookup_by_doc(self, doc_num: str) -> tuple[dict | None, str]:
        # Layer 0: Exact doc number match via deeds index (highest confidence)
        for v in _clerk_id_variants(doc_num):
            acct = self._by_clerk.get(v)
            if acct:
                entry = self._by_acct.get(acct)
                if entry:
                    return entry, "high"
        return None, "none"

    def lookup_legal(self, legal: str) -> tuple[dict, str] | tuple[None, None]:
        """Layer 1: Match by legal description. Returns (entry, confidence)."""
        if not legal:
            return None, None
        lk = _legal_key(legal)
        if lk:
            hit = self._by_legal.get(lk)
            if hit:
                return hit, "high"
        # Looser match — just subdivision + lot
        p = _parse_legal(legal)
        if p.get('sub') and p.get('lot'):
            loose_key = f"{p['sub'][:40]}|{p['lot']}"
            hit = self._by_legal.get(loose_key)
            if hit:
                return hit, "medium"
        return None, None

    def lookup_name(self, name: str) -> tuple[dict, str] | tuple[None, None]:
        """Layer 3: Match by owner name."""
        if not name:
            return None, None
        for v in _variants(name):
            hits = self._by_name.get(v)
            if hits:
                return hits[0], "medium"
        return None, None

    def lookup(self, doc_num: str, name: str, legal: str,
               grantee: str, cat: str) -> tuple[dict | None, str]:
        # Layer 0: Doc number -> deeds.txt -> acct -> address (deterministic)
        hit, conf = self.lookup_by_doc(doc_num)
        if hit:
            return hit, conf

        # Layer 1: Legal description index
        hit, conf = self.lookup_legal(legal)
        if hit:
            return hit, conf

        # Layer 2: For LP/foreclosure grantee is the homeowner
        if cat in ("LP", "NOFC", "JUD", "LIEN") and grantee:
            hit, conf = self.lookup_name(grantee)
            if hit:
                return hit, "low"

        # Layer 3: Grantor name (least reliable — only use if person not corp)
        if name and _is_person(name):
            hit, conf = self.lookup_name(name)
            if hit:
                return hit, "low"

        return None, "none"


def hcad_url(name: str, acct: str = "") -> str:
    """Build a direct HCAD property search URL."""
    if acct:
        return f"https://public.hcad.org/records/details.asp?crypt=&acct={acct}&taxyear=2025&type=real"
    if name:
        encoded = re.sub(r'\s+', '+', name.strip()[:40])
        return f"https://public.hcad.org/records/Real.asp?taxyear=2025&ownername={encoded}&county=harris"
    return ""


# ── Google Drive download ─────────────────────────────────────────────────────

def _cache_fresh(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < (CACHE_MAX_DAYS * 86400)


def download_gdrive(session: requests.Session, file_id: str, dest: Path) -> bool:
    if not file_id: return False
    log.info("Downloading Google Drive file %s ...", file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Initial request
        r = session.get(
            f"https://drive.google.com/uc?export=download&id={file_id}",
            timeout=60, allow_redirects=True
        )
        log.info("  Initial: HTTP %d, content-type: %s",
                 r.status_code, r.headers.get("content-type","?")[:50])

        # Large file confirmation
        if b"confirm" in r.content[:3000] or "text/html" in r.headers.get("content-type",""):
            log.info("  Got confirmation page — extracting token ...")
            token = re.search(rb'confirm=([^&"\']+)', r.content)
            uuid  = re.search(rb'uuid=([^&"\']+)', r.content)
            if token:
                r = session.get(
                    f"https://drive.google.com/uc?export=download&confirm={token.group(1).decode()}&id={file_id}",
                    timeout=600, stream=True
                )
            elif uuid:
                r = session.get(
                    f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t&uuid={uuid.group(1).decode()}",
                    timeout=600, stream=True
                )
            else:
                r = session.get(
                    f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t",
                    timeout=600, stream=True
                )

        ct = r.headers.get("content-type", "")
        log.info("  Download content-type: %s", ct[:60])
        if "text/html" in ct:
            log.error("  Still getting HTML — GDrive blocked download")
            log.error("  Response preview: %s", r.content[:300].decode("utf-8","replace"))
            return False

        if r.status_code == 200:
            written = 0
            with dest.open("wb") as f:
                for chunk in r.iter_content(1024*1024):
                    if chunk: f.write(chunk); written += len(chunk)
            mb = round(written/1_000_000, 1)
            log.info("  Saved %s MB → %s", mb, dest)
            return mb > 0.5
        else:
            log.warning("  HTTP %d", r.status_code)
            return False
    except Exception as e:
        log.error("  Download error: %s", e)
        return False


def load_parcel_db(session: requests.Session) -> ParcelDB:
    db = ParcelDB()

    # Load real_acct.txt (owner names + addresses)
    if _cache_fresh(REAL_ACCT_CACHE):
        log.info("Using cached real_acct.txt")
    elif GDRIVE_REAL_ACCT_ID:
        ok = download_gdrive(session, GDRIVE_REAL_ACCT_ID, REAL_ACCT_CACHE)
        if not ok:
            log.warning("real_acct.txt download failed.")
    else:
        log.warning("No GDRIVE_REAL_ACCT_ID set.")

    if REAL_ACCT_CACHE.exists():
        db.load_real_acct(REAL_ACCT_CACHE)

    # Load deeds.txt (clerk doc number -> acct mapping)
    if _cache_fresh(DEEDS_CACHE):
        log.info("Using cached deeds.txt")
    elif GDRIVE_DEEDS_ID:
        ok = download_gdrive(session, GDRIVE_DEEDS_ID, DEEDS_CACHE)
        if not ok:
            log.warning("deeds.txt download failed.")
    else:
        log.warning("No GDRIVE_DEEDS_ID set.")

    if DEEDS_CACHE.exists():
        db.load_deeds(DEEDS_CACHE)

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

CORP_SKIP = {"BANK","TRUST","CORP","LLC","INC","MORTGAGE","ELECTRONIC",
              "REGISTRATION","SYSTEMS","SERIES","NATIONAL","FEDERAL","AMERICA",
              "FINANCE","CAPITAL","INVESTMENT","SERVICES","FUNDING","ASSOCIATION",
              "HOLDINGS","PROPERTIES","REALTY","FINANCIAL","SOLUTIONS"}

def _is_person(name: str) -> bool:
    n = name.upper()
    return not any(s in n for s in CORP_SKIP) and len(name.split()) >= 2

def _extract_all_names(raw: str) -> tuple[str, str]:
    """Return (best_grantor, best_grantee) from a concatenated cell."""
    if not raw:
        return "", ""
    if "Grantor:" not in raw and "Grantee:" not in raw:
        return raw.strip(), ""

    grantors = re.findall(r"Grantor:([^G]+?)(?=Grantor:|Grantee:|$)", raw)
    grantees = re.findall(r"Grantee:([^G]+?)(?=Grantor:|Grantee:|$)", raw)

    grantors = [n.strip() for n in grantors if n.strip()]
    grantees = [n.strip() for n in grantees if n.strip()]

    # Pick best grantor — prefer a person over a corp
    best_grantor = ""
    for n in grantors:
        if _is_person(n):
            best_grantor = n; break
    if not best_grantor and grantors:
        best_grantor = grantors[0]

    # Pick best grantee — prefer a person over a corp
    best_grantee = ""
    for n in grantees:
        if _is_person(n):
            best_grantee = n; break
    if not best_grantee and grantees:
        best_grantee = grantees[0]

    return best_grantor, best_grantee

def clean_grantor(raw):
    if not raw: return ""
    g, _ = _extract_all_names(raw)
    return g or raw.strip()

def clean_grantee(raw):
    if not raw: return ""
    _, g = _extract_all_names(raw)
    return g

def parse_row(cells, headers, doc_type, cat, cat_label):
    def col_exact(*names):
        # Exact header match
        for nm in names:
            for i,h in enumerate(headers):
                if h == nm and i<len(cells):
                    v=cells[i].get_text(strip=True)
                    if v: return v
        return ""

    def col(*names):
        # Partial header match — longer names take priority
        for nm in sorted(names, key=len, reverse=True):
            for i,h in enumerate(headers):
                if nm in h and i<len(cells):
                    v=cells[i].get_text(strip=True)
                    if v: return v
        return ""

    # Doc number — try exact matches first, then partial
    doc_num = (col_exact("FILE NUMBER","FILE NO","DOC NUMBER","DOC NO","INSTRUMENT NO") or
               col("FILE NO","FILE NUM","DOC NO","DOC NUM","FILM CODE","FILE"))

    # Validate doc_num — must look like a document number not a name
    # Real doc numbers: RP-2026-151928, 2026-RP-151928, numeric strings
    if doc_num and not re.match(r'^[\dA-Z]{2,}-[\d-]+$|^\d+$', doc_num.upper().replace(' ','')):
        # Doesn't look like a doc number — try finding one in all cells
        for i, cell in enumerate(cells):
            txt = cell.get_text(strip=True)
            if re.match(r'^RP-\d{4}-\d+$|^\d{4}-RP-\d+$|^\d{6,}$', txt):
                doc_num = txt
                break

    filed  =col("DATE FILED","DATE","FILED","RECORD DATE","REC DATE")
    grantor=col("GRANTOR","OWNER","FROM","SELLER")
    grantee=col("GRANTEE","TO","BUYER","LENDER")
    legal  =col("LEGAL DESCRIPTION","LEGAL","DESCRIPTION","SUBDIV","ABSTRACT")
    amt_s  =col("AMOUNT","AMT","VALUE","CONSIDER")
    # Instrument type — avoid matching "FILE NUMBER" as "NUMBER"
    dtype  =col_exact("INSTRUMENT TYPE","DOC TYPE","TYPE") or col("INSTR TYPE","INST TYPE") or doc_type
    if "Grantor:" in grantor or "Grantee:" in grantor:
        grantor, grantee_from_cell = _extract_all_names(grantor)
        if not grantee: grantee = grantee_from_cell
    if "Grantor:" in grantee or "Grantee:" in grantee:
        _, grantee = _extract_all_names(grantee)
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
            "clerk_url":clerk_url,"match_confidence":"none",
            "hcad_url":"","flags":[],"score":0}

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
        await page.goto(CLERK_BASE,wait_until="domcontentloaded",timeout=45_000)
        await page.wait_for_load_state("networkidle",timeout=20_000)
        await asyncio.sleep(1)
    except Exception as e:
        log.warning("  Nav %s: %s",doc_type,e); return records

    # Debug form on first type
    if doc_type=="LP":
        try:
            inputs=await page.evaluate("""() => Array.from(
                document.querySelectorAll('input,select,button'))
                .filter(e=>e.offsetParent!==null)
                .map(e=>({tag:e.tagName,id:e.id,name:e.name,type:e.type||'',
                          placeholder:e.placeholder||''}))
                .filter(e=>e.id||e.name)""")
            log.info("  FORM: %s", json.dumps(inputs))
        except: pass

    # Fill form using exact field IDs from portal inspection
    # Field IDs: ctl00_ContentPlaceHolder1_txtFileNo (known)
    # Date/Type IDs — filled via JS evaluate for reliability

    filled = await page.evaluate(f"""() => {{
        // Helper to set a field value and fire events
        function set(id, val) {{
            const el = document.getElementById(id) ||
                       document.querySelector('[name="' + id + '"]');
            if (!el) return false;
            el.value = val;
            el.dispatchEvent(new Event('input', {{bubbles:true}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            return true;
        }}

        const results = {{}};

        // Try all known date field ID patterns
        const dateFromIds = ['ctl00_ContentPlaceHolder1_txtFrom'];
        const dateToIds   = ['ctl00_ContentPlaceHolder1_txtTo'];
        const itIds       = ['ctl00_ContentPlaceHolder1_txtInstrument'];

        for (const id of dateFromIds) {{
            if (set(id, '{start_date}')) {{ results.dateFrom = id; break; }}
        }}
        for (const id of dateToIds) {{
            if (set(id, '{end_date}')) {{ results.dateTo = id; break; }}
        }}

        // Set instrument type — txtInstrument is a text input
        for (const id of itIds) {{
            if (set(id, '{doc_type}')) {{
                results.instrType = id + '=' + '{doc_type}';
                break;
            }}
        }}

        // If no exact ID matched, try by input type/position
        if (!results.dateFrom) {{
            const inputs = Array.from(document.querySelectorAll('input[type=text]'))
                .filter(e => e.offsetParent !== null);
            if (inputs.length >= 2) {{
                inputs[0].value = '{start_date}';
                inputs[1].value = '{end_date}';
                results.dateFrom = 'positional[0]';
                results.dateTo   = 'positional[1]';
            }}
        }}

        return JSON.stringify(results);
    }}""")
    log.info("  Form fill: %s", filled)

    # Submit — exact ID confirmed from portal inspection
    for sel in [
        "#ctl00_ContentPlaceHolder1_btnSearch",
        "input[id='ctl00_ContentPlaceHolder1_btnSearch']",
        "input[id*='btnSearch']",
        "input[type='submit']",
        "button[type='submit']",
        "input[value='Search']",
    ]:
        try:
            el=page.locator(sel).first
            if await el.count()>0:
                await el.click(timeout=5000)
                log.info("  Submitted: %s", sel)
                break
        except: pass

    try: await page.wait_for_load_state("networkidle",timeout=25_000)
    except: pass
    await asyncio.sleep(2)

    for pg in range(1,51):
        recs=await get_page_records(page,doc_type,cat,cat_label)
        if not recs: break
        records.extend(recs)
        log.info("  Page %d: %d rows",pg,len(recs))
        try:
            await page.click("a:has-text('Next'),input[value='Next'],a[id*='Next']",timeout=4000)
            await page.wait_for_load_state("networkidle",timeout=20_000)
            await asyncio.sleep(1)
        except: break

    log.info("  → %d for %s",len(records),doc_type)
    return records


async def scrape_foreclosures(page):
    records=[]
    log.info("  Foreclosures ...")
    try:
        await page.goto(CLERK_FRCL,wait_until="domcontentloaded",timeout=45_000)
        await page.wait_for_load_state("networkidle",timeout=20_000)
        await asyncio.sleep(2)
    except Exception as e:
        log.warning("  FRCL: %s",e); return records
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
    if not HAS_PLAYWRIGHT: log.error("No Playwright"); return []
    all_records=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"])
        ctx=await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":800})
        ctx.on("dialog",lambda d: asyncio.ensure_future(d.dismiss()))
        page=await ctx.new_page()
        log.info("Warming up session ...")
        try:
            await page.goto(CLERK_HOME,wait_until="domcontentloaded",timeout=30_000)
            await page.wait_for_load_state("networkidle",timeout=15_000)
            await asyncio.sleep(3)
            log.info("Session: %s",await page.title())
        except Exception as e:
            log.warning("Warmup: %s",e)
        for doc_type in DOC_TYPE_MAP:
            if doc_type=="NOFC": continue
            for attempt in range(1,4):
                try:
                    recs=await scrape_type(page,doc_type,start_date,end_date)
                    all_records.extend(recs); await asyncio.sleep(1); break
                except Exception as e:
                    log.warning("Attempt %d %s: %s",attempt,doc_type,e)
                    await asyncio.sleep(3*attempt)
        try: all_records.extend(await scrape_foreclosures(page))
        except Exception as e: log.warning("FRCL: %s",e)
        await browser.close()
    return all_records

# ── 4-layer enrichment ────────────────────────────────────────────────────────

def enrich(records, db: ParcelDB):
    counts={"high":0,"medium":0,"low":0,"none":0}
    for r in records:
        hit, conf = db.lookup(
            doc_num = r.get("doc_num",""),
            name    = r.get("owner",""),
            legal   = r.get("legal",""),
            grantee = r.get("grantee",""),
            cat     = r.get("cat",""),
        )
        if hit:
            for k,v in hit.items():
                if v and k != "hcad_acct": r[k]=v
            r["match_confidence"] = conf
            r["hcad_url"] = hcad_url(r.get("owner",""), hit.get("hcad_acct",""))
        else:
            r["match_confidence"] = "none"
            r["hcad_url"] = hcad_url(r.get("owner",""))
        counts[conf] += 1

    log.info("Enrichment: high=%d medium=%d none=%d",
             counts["high"], counts["medium"], counts["none"])
    return records

# ── Pipeline ─────────────────────────────────────────────────────────────────

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
        log.info("Saved → %s",p)

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
    log.info("GHL CSV → %s",GHL_CSV_PATH)

# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    end_dt=datetime.now(); start_dt=end_dt-timedelta(days=LOOKBACK_DAYS)
    start=start_dt.strftime("%m/%d/%Y"); end=end_dt.strftime("%m/%d/%Y")
    iso_s=start_dt.strftime("%Y-%m-%d"); iso_e=end_dt.strftime("%Y-%m-%d")
    log.info("="*60)
    log.info("Harris County Lead Scraper — %s to %s",start,end)
    log.info("="*60)
    session=requests.Session()
    session.headers["User-Agent"]="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
    parcel_db=load_parcel_db(session)
    records=await scrape_all(start,end)
    log.info("Raw: %d",len(records))
    records=dedupe(records)
    records=enrich(records,parcel_db)
    records=apply_scores(records)
    records.sort(key=lambda r: r.get("score",0),reverse=True)
    save_json(records,iso_s,iso_e)
    save_csv(records)
    log.info("="*60)
    log.info("Done. %d leads.",len(records))
    log.info("="*60)

if __name__=="__main__":
    asyncio.run(main())
