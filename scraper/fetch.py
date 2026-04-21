"""
Harris County Motivated Seller Lead Scraper
Playwright-based with proper session handling and HCAD address lookup
via the public HCAD search API instead of bulk download.
"""
from __future__ import annotations
import asyncio, csv, io, json, logging, os, re, sys, time, zipfile
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

# HCAD public property search API — no auth required
HCAD_SEARCH   = "https://public.hcad.org/records/details.asp"
HCAD_API      = "https://public.hcad.org/records/Real.asp"

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

# ── HCAD address lookup via public search API ─────────────────────────────────

def _hcad_lookup_by_name(session: requests.Session, owner_name: str) -> dict | None:
    """Search HCAD public records by owner name."""
    if not owner_name or len(owner_name) < 3:
        return None
    try:
        r = session.get(
            "https://public.hcad.org/records/Real.asp",
            params={"taxyear": "2025", "ownername": owner_name, "county": "harris"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        # Find first result row
        rows = soup.find_all("tr")
        for tr in rows[1:]:
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            # Typical columns: acct, owner, site_addr, mailing_addr
            if len(texts) >= 3 and texts[2]:
                addr_parts = texts[2].split(",")
                site_addr  = addr_parts[0].strip() if addr_parts else ""
                return {
                    "prop_address": site_addr,
                    "prop_city":    "Houston",
                    "prop_state":   "TX",
                    "prop_zip":     "",
                    "mail_address": texts[3] if len(texts) > 3 else "",
                    "mail_city":    "",
                    "mail_state":   "TX",
                    "mail_zip":     "",
                }
    except Exception as e:
        log.debug("HCAD lookup error for %s: %s", owner_name, e)
    return None


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
        names = re.findall(r"Grantor:([^G]+?)(?=Grantor:|Grantee:|$)", raw)
        if names:
            skip={"BANK","TRUST","CORP","LLC","INC","MORTGAGE","ELECTRONIC",
                  "REGISTRATION","SYSTEMS","SERIES","NATIONAL","FEDERAL","AMERICA"}
            for n in names:
                n=n.strip()
                if n and not any(s in n.upper() for s in skip):
                    return n
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
    log.debug("  Headers: %s", headers[:8])
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

    # Navigate to search page fresh each time
    try:
        await page.goto(CLERK_BASE, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await asyncio.sleep(1)
    except Exception as e:
        log.warning("  Nav error %s: %s", doc_type, e)
        return records

    # Log page title to confirm we landed correctly
    title = await page.title()
    log.info("  Page: %s", title[:60])

    # Dump all input/select elements for debugging on first run
    if doc_type == "LP":
        inputs = await page.evaluate("""() => {
            const els = document.querySelectorAll('input,select');
            return Array.from(els).map(e => ({
                tag: e.tagName, id: e.id, name: e.name,
                type: e.type, placeholder: e.placeholder
            })).filter(e => e.id || e.name);
        }""")
        log.info("  Form elements: %s", json.dumps(inputs[:20]))

    # Try multiple strategies to fill the form
    filled = False

    # Strategy 1: Fill by visible label text using Playwright locator
    try:
        # Date From
        await page.get_by_label(re.compile(r"date.*from|from.*date|start", re.I)).first.fill(start_date, timeout=3000)
        await page.get_by_label(re.compile(r"date.*to|to.*date|end", re.I)).first.fill(end_date, timeout=3000)
        # Instrument type
        await page.get_by_label(re.compile(r"instrument|type|doc", re.I)).first.select_option(value=doc_type, timeout=3000)
        filled = True
        log.info("  Filled via label")
    except: pass

    # Strategy 2: Fill by placeholder
    if not filled:
        try:
            await page.get_by_placeholder(re.compile(r"from|start|begin", re.I)).first.fill(start_date, timeout=3000)
            await page.get_by_placeholder(re.compile(r"to|end", re.I)).first.fill(end_date, timeout=3000)
            filled = True
            log.info("  Filled via placeholder")
        except: pass

    # Strategy 3: Try all inputs by position
    if not filled:
        try:
            all_inputs = await page.query_selector_all("input[type='text'], input:not([type])")
            date_inputs = []
            for inp in all_inputs:
                vis = await inp.is_visible()
                if vis:
                    date_inputs.append(inp)
            if len(date_inputs) >= 2:
                await date_inputs[0].fill(start_date)
                await date_inputs[1].fill(end_date)
                filled = True
                log.info("  Filled via positional inputs")
        except: pass

    # Set instrument type via JS if select exists
    try:
        result = await page.evaluate(f"""() => {{
            const selects = document.querySelectorAll('select');
            for (const s of selects) {{
                for (const opt of s.options) {{
                    if (opt.value === '{doc_type}' || opt.text.includes('{doc_type}')) {{
                        s.value = opt.value;
                        s.dispatchEvent(new Event('change', {{bubbles:true}}));
                        return 'set:' + s.id + '=' + opt.value;
                    }}
                }}
            }}
            return 'not_found';
        }}""")
        log.info("  Instrument type JS: %s", result)
    except: pass

    # Submit
    submitted = False
    for sel in ["input[type='submit']","button[type='submit']",
                "input[value='Search']","button:has-text('Search')",
                "input[id*='Search']","input[id*='search']"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=5000)
                submitted = True
                log.info("  Clicked submit: %s", sel)
                break
        except: pass

    if not submitted:
        log.warning("  Could not submit search for %s", doc_type)
        return records

    try:
        await page.wait_for_load_state("networkidle", timeout=25_000)
    except: pass
    await asyncio.sleep(2)

    # Paginate
    for pg in range(1, 51):
        recs = await get_page_records(page, doc_type, cat, cat_label)
        if not recs:
            break
        records.extend(recs)
        log.info("  Page %d: %d records", pg, len(recs))
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

    log.info("  → %d total for %s", len(records), doc_type)
    return records


async def scrape_foreclosures(page):
    records=[]
    log.info("  Foreclosures ...")
    try:
        await page.goto(CLERK_FRCL, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await asyncio.sleep(2)
    except Exception as e:
        log.warning("  FRCL error: %s", e); return records
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
    log.info("  → %d foreclosures", len(records))
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

        # Warm up — establish session cookie
        log.info("Warming up session ...")
        try:
            await page.goto(CLERK_HOME, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)
            await asyncio.sleep(3)
            log.info("Session ready. Title: %s", await page.title())
        except Exception as e:
            log.warning("Warmup error: %s", e)

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
            log.warning("Foreclosure error: %s", e)

        await browser.close()
    return all_records

# ── HCAD enrichment via public API ───────────────────────────────────────────

def enrich(records, session):
    log.info("Enriching %d records via HCAD public search ...", len(records))
    n=0
    for r in records:
        owner=r.get("owner","").strip()
        if not owner or owner.lower() in ("","unknown"):
            continue
        try:
            hit=_hcad_lookup_by_name(session, owner)
            if hit:
                for k,v in hit.items():
                    if v: r[k]=v
                n+=1
                time.sleep(0.3)  # polite delay
        except Exception as e:
            log.debug("Enrich error for %s: %s", owner, e)
    log.info("Enriched %d/%d", n, len(records))
    return records

# ── Pipeline ─────────────────────────────────────────────────────────────────

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

    records=await scrape_all(start, end)
    log.info("Raw: %d", len(records))
    records=dedupe(records)
    records=enrich(records, session)
    records=apply_scores(records)
    records.sort(key=lambda r: r.get("score",0), reverse=True)
    save_json(records, iso_s, iso_e)
    save_csv(records)
    log.info("="*60)
    log.info("Done. %d leads.", len(records))
    log.info("="*60)

if __name__=="__main__":
    asyncio.run(main())
