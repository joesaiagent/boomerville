"""
Boomerville API — FastAPI backend
Targets long-term homeowners across Monmouth County, NJ
(Freehold, Howell, Wall, and Millstone Townships)
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import random
from datetime import datetime
from pathlib import Path

import anthropic
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
LEADS_FILE = BASE_DIR / "leads.json"
DATA_DIR.mkdir(exist_ok=True)

# ─── App setup ────────────────────────────────────────────────────────────────
app = FastAPI(title="Boomerville API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files
FRONTEND_DIR = BASE_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ─── NJ Appreciation by era ───────────────────────────────────────────────────
# Source: NJ REALTORS® historical median price data + FHFA HPI
NJ_ANNUAL_RATES = {
    (1970, 1979): 0.060,   # High-inflation 70s
    (1980, 1989): 0.075,   # NJ suburban boom
    (1990, 1995): -0.005,  # Post-S&L correction
    (1996, 2005): 0.068,   # Dot-com / pre-crash boom
    (2006, 2011): -0.025,  # Housing crash
    (2012, 2019): 0.042,   # Recovery
    (2020, 2026): 0.085,   # Pandemic surge
}

CURRENT_YEAR = 2026
MONMOUTH_ASSESSMENT_RATIO = 0.82  # Monmouth County equalization ratio (all three townships)

# Supported municipalities — district codes confirmed from live site (2026-04-16)
MUNICIPALITIES: dict[str, dict] = {
    "freehold": {
        "district": "1317",
        "name": "Freehold Township",
        "zip": "07728",
        "streets": [
            "Oak Hill Rd", "Wemrock Rd", "Elton Adelphia Rd", "Schanck Rd",
            "Kozloski Rd", "Stillwells Corner Rd", "Augusta Dr", "Tennent Rd",
            "Burlington Path Rd", "Tavern Rd", "Randolph Rd", "Park Ave",
            "Georgia Rd", "Conover Rd", "Jerseyville Ave", "Maplewood Dr",
            "Ironwood Ct", "Woodfield Rd", "Craig Rd", "Briar Hill Dr",
            "Dutch Lane Rd", "Beacon Hill Blvd", "Prospect St", "Jackson Mills Rd",
        ],
    },
    "howell": {
        "district": "1321",
        "name": "Howell Township",
        "zip": "07731",
        "streets": [
            "Preventorium Rd", "Gravel Hill Rd", "Ramtown Greenville Rd",
            "Lakewood Farmingdale Rd", "Squankum Yellowbrook Rd", "Lanes Mill Rd",
            "Lexington Ave", "Aldrich Rd", "Casino Dr", "Yellowbrook Rd",
            "Oak Glen Rd", "Colts Neck Rd", "Fairfield Rd", "Ramshorn Dr",
            "Herbertsville Rd", "Georgia Rd", "Conover Rd", "Summerville Rd",
            "Sycamore Ave", "Maxim Southard Rd",
        ],
    },
    "wall": {
        "district": "1352",
        "name": "Wall Township",
        "zip": "07719",
        "streets": [
            "Wall Allenwood Rd", "New Bedford Rd", "Belmar Blvd", "Route 34",
            "Allaire Rd", "Hospital Rd", "Lakewood Rd", "Baileys Corner Rd",
            "Atlantic Ave", "Sea Girt Ave", "Patterson Ave", "Ramshorn Dr",
            "Jumping Brook Rd", "Stony Brook Rd", "Allenwood Rd", "Herbertsville Rd",
            "Old Mill Rd", "Collingwood Rd", "Essex Rd", "Valley Rd",
        ],
    },
    "millstone": {
        "district": "1333",
        "name": "Millstone Township",
        "zip": "08535",
        "streets": [
            "Perrineville Rd", "Herbert Rd", "Agress Rd", "Burlington Path Rd",
            "Clarksburg Rd", "Walnford Rd", "Yellow Meeting House Rd", "Stagecoach Rd",
            "Fresh Ponds Rd", "Sweetmans Lane", "Bunting Bridge Rd", "Millstone Rd",
            "Ellisdale Rd", "Arneytown Hornerstown Rd", "Assunpink Rd",
            "Thompson Bridge Rd", "Ely Harmony Rd", "Emley Rd", "Rues Ln",
            "Borden Rd",
        ],
    },
}


# ─── Equity math ──────────────────────────────────────────────────────────────

def appreciation_factor(year_purchased: int) -> float:
    """Compound appreciation multiplier from purchase year to today."""
    factor = 1.0
    for (start, end), rate in NJ_ANNUAL_RATES.items():
        overlap_start = max(year_purchased, start)
        overlap_end = min(CURRENT_YEAR, end + 1)
        years = max(0, overlap_end - overlap_start)
        factor *= (1 + rate) ** years
    return factor


def estimate_equity(assessed_value: float, year_purchased: int) -> dict:
    """
    Derive current market value and equity from current assessed value.
    NJ assessed values represent a fraction of market value (equalization ratio).
    """
    estimated_current_value = assessed_value / MONMOUTH_ASSESSMENT_RATIO
    years_owned = CURRENT_YEAR - year_purchased
    factor = appreciation_factor(year_purchased)

    # Original purchase price (approximate)
    original_price = estimated_current_value / factor

    # Remaining mortgage estimate (30-yr at ~historical rate)
    if years_owned >= 30:
        remaining_mortgage = 0.0
        has_mortgage = False
    else:
        loan = original_price * 0.80          # 80% LTV at purchase
        monthly_rate = 0.08 / 12              # historical ~8%
        n_total = 360                         # 30-year loan
        n_paid = min(years_owned * 12, n_total)
        if n_paid >= n_total:
            remaining_mortgage = 0.0
        else:
            remaining_mortgage = loan * (
                ((1 + monthly_rate) ** n_total - (1 + monthly_rate) ** n_paid)
                / ((1 + monthly_rate) ** n_total - 1)
            )
        has_mortgage = remaining_mortgage > 5_000

    estimated_equity = estimated_current_value - remaining_mortgage
    equity_pct = (estimated_equity / estimated_current_value * 100) if estimated_current_value > 0 else 0

    return {
        "estimated_current_value": round(estimated_current_value),
        "estimated_equity": round(estimated_equity),
        "equity_percentage": round(equity_pct, 1),
        "has_mortgage": has_mortgage,
    }


def build_tags(year_purchased: int, equity_pct: float) -> list[str]:
    tags = []
    years_owned = CURRENT_YEAR - year_purchased
    if equity_pct >= 70:
        tags.append("High Equity")
    if year_purchased < 1985:
        tags.append("Legacy Owner")
    if years_owned >= 30:
        tags.append("Long-term Owner")
    if equity_pct >= 95:
        tags.append("Free & Clear")
    return tags


# ─── Claude client helper ─────────────────────────────────────────────────────

def claude_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set in environment")
    return anthropic.Anthropic(api_key=api_key)


# ─── Monmouth County scraper ──────────────────────────────────────────────────
#
# Flow:
#   1. POST to inf.cgi with advanced date filter (sale_to ≤ max_purchase_year)
#      → returns up to 1000 properties that last sold before that year
#   2. Extract m4.cgi detail-page links from the results list
#   3. Fetch detail pages concurrently (semaphore-limited) and parse each one
#
# Verified correct parameters via live inspection of the site (2026-04-16):
#   - District 1317 = Freehold Township (not 0913 which is Hudson County)
#   - Form posts to inf.cgi, not prc6.cgi
#   - Advanced search (adv=2) exposes sale_from / sale_to date filters
#   - Property detail is on m4.cgi, not the list page

MONMOUTH_BASE = "https://tax1.co.monmouth.nj.us/cgi-bin"


async def scrape_monmouth_records(
    max_purchase_year: int,
    municipality_key: str = "freehold",
    max_results: int = 30,
) -> list[dict]:
    """
    Scrape real Freehold Township NJ property records owned since max_purchase_year.
    Returns empty list on any failure so the caller can use the fallback.
    """
    mun = MUNICIPALITIES.get(municipality_key, MUNICIPALITIES["freehold"])
    list_payload = {
        "ms_user": "monm",
        "passwd": "data",
        "district": mun["district"],
        "srch_type": "1",       # Current Owners / Assessment List
        "adv": "2",             # Advanced search (exposes date filter)
        "out_type": "1",        # Single-line list
        "ms_ln": "1000",        # Max results per page
        "p_loc": "",
        "owner": "",
        "block": "",
        "lot": "",
        "qual": "",
        "street": "",
        "city": "",
        "class": "2",           # Residential only
        "sale_from": "1800-01-01",
        "sale_to": f"{max_purchase_year}-12-31",
        "sr1a_f": "0",
        "cl_type": "0",
        "zone": "",
        "book": "",
        "page": "",
        "built_f": "0",
        "built_t": "0",
        "sqft_f": "0",
        "sqft_t": "0",
        "land_f": "0",
        "land_t": "0",
        "impr_f": "0",
        "impr_t": "0",
        "net_f": "0",
        "net_t": "0",
        "sale_f": "0",
        "sale_t": "0",
    }

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Step 1: get the list of matching properties
            resp = await client.post(f"{MONMOUTH_BASE}/inf.cgi", data=list_payload)
            if resp.status_code != 200 or len(resp.text) < 500:
                return []

            soup = BeautifulSoup(resp.text, "lxml")
            detail_links = [
                a["href"] for a in soup.find_all("a")
                if a.get("href", "").startswith("m4.cgi")
            ]
            if not detail_links:
                return []

            # Shuffle so we get geographic diversity, not just Block 1
            rng = random.Random(42)
            rng.shuffle(detail_links)
            # Fetch 2× what we need in case some pages are unparseable
            sample = detail_links[:min(max_results * 2, 80)]

            # Step 2: fetch detail pages concurrently, max 5 at a time
            sem = asyncio.Semaphore(5)

            async def fetch_one(link: str) -> dict | None:
                async with sem:
                    await asyncio.sleep(0.15)   # be polite to the county server
                    try:
                        r = await client.get(f"{MONMOUTH_BASE}/{link}", timeout=15)
                        if r.status_code != 200:
                            return None
                        return _parse_m4_detail(r.text, max_purchase_year, mun)
                    except Exception:
                        return None

            results = await asyncio.gather(*[fetch_one(lnk) for lnk in sample])
            properties = [p for p in results if p is not None]
            return properties[:max_results]

    except Exception:
        return []


def _parse_m4_detail(html: str, max_purchase_year: int, mun: dict | None = None) -> dict | None:
    """
    Parse a single m4.cgi property detail page.

    Page structure (verified 2026-04-16):
      Table 0 — main property fields
        Row 0:  Block: | val | Prop Loc: | val | Owner: | val | Square Ft: | val
        Row 1:  Lot:   | val | District: | val | Street:| val | Year Built:| val
        Row 2:  Qual:  | val | Class:    | val | City St| val | Style:     | val
        Row 8:  Zone:  | val | Map Page: | val | Acreage| val | Taxes:     | val
        Row 10: Sale Date: | MM/DD/YY | Book: | val | Price: | val | ...
      Table 1 — Sr1a (deed history list)
      Table 2 — Tax-List-History
        Row 0:  "TAX-LIST-HISTORY"
        Row 1:  column headers
        Row 2:  2026 | location | land | exemption | total_assessed | class
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        if len(tables) < 3:
            return None

        def cell(t: int, r: int, c: int) -> str:
            rows = tables[t].find_all("tr")
            if r >= len(rows):
                return ""
            cells = rows[r].find_all("td")
            if c >= len(cells):
                return ""
            return cells[c].get_text(strip=True).replace("\xa0", " ").strip()

        # ── Main fields ──────────────────────────────────────────────────────
        address_street  = cell(0, 0, 3)
        owner_raw       = cell(0, 0, 5)
        sq_ft_raw       = cell(0, 0, 7)
        year_built_raw  = cell(0, 1, 7)
        city_state_raw  = cell(0, 2, 5)
        acreage_raw     = cell(0, 8, 3)
        sale_date_raw   = cell(0, 10, 1)

        # ── Sale date → year purchased ───────────────────────────────────────
        if not sale_date_raw or sale_date_raw == "00/00/00" or "/" not in sale_date_raw:
            return None
        parts = sale_date_raw.split("/")
        if len(parts) != 3:
            return None
        yy = int(parts[2])
        # 2-digit year: >25 → 1900s, ≤25 → 2000s  (avoids the Y2K split at 2025)
        year_purchased = (1900 + yy) if yy > 25 else (2000 + yy)
        if year_purchased > max_purchase_year:
            return None

        # ── Owner / address sanity checks ─────────────────────────────────────
        owner_name = re.sub(r"\s*&nbsp.*", "", owner_raw, flags=re.I).strip()
        owner_name = re.sub(r"\s+", " ", owner_name).title()
        if not owner_name or owner_name.lower() in ("unknown", ""):
            return None

        if not address_street:
            return None

        mun = mun or MUNICIPALITIES["freehold"]
        zip_code = mun["zip"]
        zip_match = re.search(r"0\d{4}", city_state_raw)
        if zip_match:
            zip_code = zip_match.group()

        address = f"{address_street.title()}, {mun['name']}, NJ {zip_code}"

        # ── 2026 assessed value from tax history (Table 2, row 2) ────────────
        assessed_value = 0.0
        t2_rows = tables[2].find_all("tr")
        for row in t2_rows[2:10]:
            cols = [td.get_text(strip=True).replace(",", "").replace("\xa0", "") for td in row.find_all("td")]
            if cols and cols[0] in ("2026", "2025") and len(cols) >= 5:
                try:
                    v = float(cols[4])
                    if v > 0:
                        assessed_value = v
                        break
                except ValueError:
                    pass
        if assessed_value < 10_000:
            return None

        # ── Numeric fields ────────────────────────────────────────────────────
        try:
            sq_ft = int(sq_ft_raw)
        except (ValueError, TypeError):
            sq_ft = 0

        try:
            lot_size_acres = round(float(acreage_raw), 4)
        except (ValueError, TypeError):
            lot_size_acres = 0.0

        # Estimate bedrooms from sq_ft (NJ tax records don't include bed count)
        if sq_ft < 1200:
            bedrooms, bathrooms = 2, 1.0
        elif sq_ft < 1600:
            bedrooms, bathrooms = 3, 1.5
        elif sq_ft < 2200:
            bedrooms, bathrooms = 3, 2.0
        elif sq_ft < 3000:
            bedrooms, bathrooms = 4, 2.5
        else:
            bedrooms, bathrooms = 5, 3.0

        # ── Equity + tags ─────────────────────────────────────────────────────
        equity_data = estimate_equity(assessed_value, year_purchased)
        tags = build_tags(year_purchased, equity_data["equity_percentage"])

        return {
            "address": address,
            "owner_name": owner_name,
            "year_purchased": year_purchased,
            "years_owned": CURRENT_YEAR - year_purchased,
            "assessed_value": assessed_value,
            **equity_data,
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "sq_ft": sq_ft,
            "lot_size_acres": lot_size_acres,
            "tags": tags,
            "source": "monmouth_county_records",
        }
    except Exception:
        return None


# ─── Realistic fallback data ──────────────────────────────────────────────────

def generate_fallback_properties(municipality_key: str, min_years_owned: int, count: int = 25) -> list[dict]:
    """
    Generate deterministic, realistic-looking NJ property data for the given municipality.
    Used when live scraping is unavailable.
    """
    mun = MUNICIPALITIES.get(municipality_key, MUNICIPALITIES["freehold"])
    # Use municipality key as part of seed so each township gets distinct data
    rng = random.Random(hash(municipality_key) & 0xFFFF)

    first_names = [
        "Robert", "Dorothy", "James", "Patricia", "Charles", "Barbara",
        "William", "Susan", "Richard", "Linda", "Michael", "Nancy",
        "Thomas", "Karen", "Gary", "Betty", "Larry", "Helen",
        "Kenneth", "Carol", "Donald", "Sandra", "George", "Donna",
        "Ronald", "Ruth", "Edward", "Sharon", "Harold", "Judith",
    ]
    last_names = [
        "Murphy", "O'Brien", "Kowalski", "Patel", "Johnson", "Williams",
        "Brown", "Davis", "Miller", "Wilson", "Moore", "Taylor",
        "Anderson", "Thompson", "Garcia", "Martinez", "Robinson",
        "Clark", "Rodriguez", "Lewis", "Conover", "Schanck",
        "Wemrock", "Tennent", "Kozloski",
    ]

    max_purchase_year = CURRENT_YEAR - min_years_owned
    properties = []

    for i in range(count):
        year_purchased = rng.randint(1968, max_purchase_year)
        house_num = rng.randint(10, 398)
        street = rng.choice(mun["streets"])
        address = f"{house_num} {street}, {mun['name']}, NJ {mun['zip']}"
        owner_name = f"{rng.choice(first_names)} {rng.choice(last_names)}"

        assessed_value = round(rng.uniform(195_000, 420_000), -3)
        equity_data = estimate_equity(assessed_value, year_purchased)
        tags = build_tags(year_purchased, equity_data["equity_percentage"])

        properties.append({
            "address": address,
            "owner_name": owner_name,
            "year_purchased": year_purchased,
            "years_owned": CURRENT_YEAR - year_purchased,
            "assessed_value": assessed_value,
            **equity_data,
            "bedrooms": rng.randint(2, 5),
            "bathrooms": rng.choice([1.0, 1.5, 2.0, 2.5, 3.0]),
            "sq_ft": rng.randint(1100, 3400),
            "lot_size_acres": round(rng.uniform(0.18, 1.85), 2),
            "tags": tags,
            "source": "realistic_fallback",
            "municipality": mun["name"],
        })

    return properties


# ═══════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "Boomerville API running. Frontend not found at ../frontend/index.html"}


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ─── /api/search  (original Claude-simulated endpoint) ───────────────────────

@app.get("/api/search")
async def search(municipality: str = "freehold", min_years_owned: int = 30):
    """Original endpoint: Claude generates simulated property records."""
    client = claude_client()
    mun = MUNICIPALITIES.get(municipality, MUNICIPALITIES["freehold"])
    max_year = CURRENT_YEAR - min_years_owned

    prompt = f"""Generate 15 realistic property records for long-term homeowners in {mun['name']}, NJ (zip: {mun['zip']}).
Each owner must have purchased in {max_year} or earlier (owned {min_years_owned}+ years).
Use real {mun['name']} street names. Values must be realistic for Monmouth County NJ.

Return a JSON array of exactly 15 objects. Each object must have these exact keys:
address, owner_name, year_purchased, years_owned, assessed_value, estimated_current_value,
estimated_equity, equity_percentage, bedrooms, bathrooms, sq_ft, lot_size_acres, has_mortgage, tags

Rules:
- address must include "{mun['name']}, NJ {mun['zip']}"
- assessed_value: $180000–$420000 (NJ assessed, ~82% of market)
- estimated_current_value: assessed_value / 0.82, then apply historical NJ appreciation
- equity_percentage >= 70 → include "High Equity" in tags
- year_purchased < 1985 → include "Legacy Owner" in tags
- years_owned >= 30 → include "Long-term Owner" in tags
- equity_percentage >= 95 → include "Free & Clear" in tags
- has_mortgage: false if years_owned >= 30

Return ONLY the raw JSON array. No markdown, no commentary."""

    msg = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    text = msg.content[0].text.strip()
    # Strip markdown fences if model wrapped the JSON
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)

    properties = json.loads(text)
    return {"source": "claude_simulated", "count": len(properties), "properties": properties}


# ─── /api/real-search  (real data with caching + fallback) ───────────────────

@app.get("/api/municipalities")
async def get_municipalities():
    """Return the list of supported municipalities."""
    return {
        k: {"name": v["name"], "zip": v["zip"]}
        for k, v in MUNICIPALITIES.items()
    }


@app.get("/api/real-search")
async def real_search(municipality: str = "freehold", min_years_owned: int = 30):
    """
    Returns real Monmouth County tax records for the given municipality.
    Pass municipality=freehold|howell|wall or municipality=all for all three.
    Falls back to realistic generated data per township if scraping fails.
    Caches results per municipality for 24 hours.
    """
    keys = list(MUNICIPALITIES.keys()) if municipality == "all" else [municipality]
    # Validate
    keys = [k for k in keys if k in MUNICIPALITIES]
    if not keys:
        keys = ["freehold"]

    max_purchase_year = CURRENT_YEAR - min_years_owned
    all_properties: list[dict] = []
    sources: list[str] = []
    notes: list[str] = []

    for key in keys:
        mun = MUNICIPALITIES[key]
        cache_path = DATA_DIR / f"cache_{key}_{min_years_owned}.json"

        # Serve from cache if fresh
        if cache_path.exists():
            age_hours = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600
            if age_hours < 24:
                cached = json.loads(cache_path.read_text())
                all_properties.extend(cached.get("properties", []))
                sources.append(cached.get("source", "cached"))
                continue

        source = "realistic_fallback"
        properties: list[dict] = []

        try:
            scraped = await scrape_monmouth_records(
                max_purchase_year=max_purchase_year,
                municipality_key=key,
            )
            if len(scraped) >= 10:
                properties = scraped
                source = "monmouth_county_records"
            else:
                notes.append(f"{mun['name']}: scraper returned {len(scraped)} records; using fallback.")
        except Exception as e:
            notes.append(f"{mun['name']}: scraper error ({e}); using fallback.")

        if len(properties) < 10:
            properties = generate_fallback_properties(key, min_years_owned, count=25)
            source = "realistic_fallback"

        properties = [p for p in properties if p.get("year_purchased", 9999) <= max_purchase_year]

        mun_result = {
            "source": source,
            "municipality": key,
            "min_years_owned": min_years_owned,
            "count": len(properties),
            "properties": properties,
            "cached": False,
        }
        cache_path.write_text(json.dumps(mun_result, indent=2))
        all_properties.extend(properties)
        sources.append(source)

    # Deduplicate by address across townships
    seen: set[str] = set()
    deduped = []
    for p in all_properties:
        addr = p.get("address", "")
        if addr not in seen:
            seen.add(addr)
            deduped.append(p)

    dominant_source = "monmouth_county_records" if "monmouth_county_records" in sources else sources[0] if sources else "realistic_fallback"

    return {
        "source": dominant_source,
        "municipalities": keys,
        "min_years_owned": min_years_owned,
        "count": len(deduped),
        "properties": deduped,
        "cached": False,
        "notes": "; ".join(notes) if notes else None,
    }


# ─── /api/draft-letter ───────────────────────────────────────────────────────

@app.post("/api/draft-letter")
async def draft_letter(body: dict):
    """
    Uses Claude to write a warm, non-pressuring outreach letter for a property owner.
    Accepts the property object in the request body.
    """
    prop = body.get("property", body)

    owner_name = prop.get("owner_name", "Homeowner")
    first_name = owner_name.split()[0] if owner_name else "Neighbor"
    address = prop.get("address", "your property")
    years_owned = prop.get("years_owned", 30)
    year_purchased = prop.get("year_purchased", 1990)
    tags = prop.get("tags", [])

    client = claude_client()

    prompt = f"""Write a warm, respectful outreach letter from Boomerville to a long-term homeowner.

Homeowner details:
- First name: {first_name}
- Address: {address}
- Years at this home: ~{years_owned} years (since ~{year_purchased})
- Property tags: {', '.join(tags) if tags else 'Long-term owner'}

About Boomerville: A small, local group based in Monmouth County, NJ that connects with long-time homeowners
who may be open to exploring their options — direct sale, learning what their home is worth, or simply a conversation.

Letter requirements:
- Address them by first name ({first_name})
- Tone: warm friendly neighbor, NOT corporate, NOT pushy, NOT salesy
- Acknowledge the significance of {years_owned} years in one home
- Briefly mention Boomerville as a local buyer/researcher group (1-2 sentences)
- Make zero-obligation crystal clear — no pressure whatsoever
- Offer a free, no-strings conversation or home value chat
- Under 200 words total
- Sign off: "Warmly, The Boomerville Team — Monmouth County, NJ"
- Do NOT mention specific dollar amounts, equity percentages, or assessed values

Return only the letter text. No subject line, no commentary."""

    msg = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    letter = msg.content[0].text.strip()
    return {
        "owner_name": owner_name,
        "address": address,
        "letter": letter,
    }


# ─── /api/save-lead ──────────────────────────────────────────────────────────

@app.post("/api/save-lead")
async def save_lead(body: dict):
    """Save a property to leads.json. Deduplicates by address."""
    prop = body.get("property", body)

    leads: list[dict] = []
    if LEADS_FILE.exists():
        leads = json.loads(LEADS_FILE.read_text())

    existing_addresses = {l.get("address") for l in leads}
    if prop.get("address") in existing_addresses:
        return {"status": "already_saved", "total_leads": len(leads)}

    prop["saved_at"] = datetime.now().isoformat()
    leads.append(prop)
    LEADS_FILE.write_text(json.dumps(leads, indent=2))

    return {"status": "saved", "total_leads": len(leads)}


# ─── /api/leads ──────────────────────────────────────────────────────────────

@app.get("/api/leads")
async def get_leads():
    """Return all saved leads."""
    if not LEADS_FILE.exists():
        return {"leads": [], "count": 0}
    leads = json.loads(LEADS_FILE.read_text())
    return {"leads": leads, "count": len(leads)}


@app.delete("/api/leads/{address}")
async def delete_lead(address: str):
    """Remove a lead by address."""
    if not LEADS_FILE.exists():
        raise HTTPException(status_code=404, detail="No leads file found")
    leads = json.loads(LEADS_FILE.read_text())
    original_count = len(leads)
    leads = [l for l in leads if l.get("address") != address]
    LEADS_FILE.write_text(json.dumps(leads, indent=2))
    removed = original_count - len(leads)
    return {"status": "removed" if removed else "not_found", "total_leads": len(leads)}
