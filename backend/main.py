"""
Boomerville API — FastAPI backend
Targets long-term homeowners in Freehold Township, NJ (07728)
"""
from __future__ import annotations

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
FREEHOLD_TWP_ASSESSMENT_RATIO = 0.82  # Monmouth County avg equalization ratio

FREEHOLD_TWP_STREETS = [
    "Oak Hill Rd", "Wemrock Rd", "Elton Adelphia Rd", "Schanck Rd",
    "Kozloski Rd", "Stillwells Corner Rd", "Augusta Dr", "Tennent Rd",
    "Burlington Path Rd", "Tavern Rd", "Randolph Rd", "Park Ave",
    "Georgia Rd", "Conover Rd", "Jerseyville Ave", "Maplewood Dr",
    "Ironwood Ct", "Woodfield Rd", "Craig Rd", "Briar Hill Dr",
    "Dutch Lane Rd", "Beacon Hill Blvd", "Prospect St", "Jackson Mills Rd",
]


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
    estimated_current_value = assessed_value / FREEHOLD_TWP_ASSESSMENT_RATIO
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

async def scrape_monmouth_records(max_purchase_year: int, max_results: int = 30) -> list[dict]:
    """
    Attempt to scrape public tax records from Monmouth County's PRC system.
    https://tax1.co.monmouth.nj.us/cgi-bin/prc6.cgi
    Returns empty list on failure — caller will use fallback.
    """
    base_url = "https://tax1.co.monmouth.nj.us/cgi-bin/prc6.cgi"
    # Freehold Township district = 0913 in Monmouth County
    query_params = {
        "ms_user": "monm",
        "passwd": "data",
        "district": "0913",
        "adv": "0",
        "out": "web",
    }

    properties: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(base_url, params=query_params)
            if resp.status_code != 200 or len(resp.text) < 200:
                return []

            soup = BeautifulSoup(resp.text, "lxml")
            rows = soup.select("table tr")[1:]  # skip header row

            for row in rows:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cols) < 5:
                    continue
                prop = _parse_prc_row(cols, max_purchase_year)
                if prop:
                    properties.append(prop)
                if len(properties) >= max_results:
                    break
    except Exception:
        return []

    return properties


def _parse_prc_row(cols: list[str], max_purchase_year: int) -> dict | None:
    """
    Best-effort parse of a PRC6 table row.
    Columns vary by county config; we handle the most common layout.
    """
    try:
        # Typical PRC6 layout: Block | Lot | Location | Owner | Class | Land | Improve | Total | Deed Date
        address_raw = cols[2] if len(cols) > 2 else ""
        owner_raw = cols[3] if len(cols) > 3 else ""
        total_str = cols[7] if len(cols) > 7 else "0"
        deed_str = cols[8] if len(cols) > 8 else ""

        if not address_raw or not owner_raw:
            return None

        # Parse deed year
        year_match = re.search(r"\b(19\d{2}|20[01]\d)\b", deed_str)
        if not year_match:
            return None
        year_purchased = int(year_match.group(1))
        if year_purchased > max_purchase_year:
            return None

        # Parse assessed value
        assessed_value = float(re.sub(r"[^\d.]", "", total_str) or "0")
        if assessed_value < 10_000:
            return None

        equity_data = estimate_equity(assessed_value, year_purchased)
        tags = build_tags(year_purchased, equity_data["equity_percentage"])

        address = f"{address_raw.title()}, Freehold Township, NJ 07728"
        owner_name = owner_raw.title()

        return {
            "address": address,
            "owner_name": owner_name,
            "year_purchased": year_purchased,
            "years_owned": CURRENT_YEAR - year_purchased,
            "assessed_value": assessed_value,
            **equity_data,
            "bedrooms": 3,
            "bathrooms": 2.0,
            "sq_ft": 1800,
            "lot_size_acres": 0.5,
            "tags": tags,
            "source": "monmouth_county_records",
        }
    except Exception:
        return None


# ─── Realistic fallback data ──────────────────────────────────────────────────

def generate_fallback_properties(zip_code: str, min_years_owned: int, count: int = 25) -> list[dict]:
    """
    Generate deterministic, realistic-looking Freehold Township NJ property data.
    Used when live scraping is unavailable.
    """
    rng = random.Random(42)  # fixed seed → stable across calls

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
        street = rng.choice(FREEHOLD_TWP_STREETS)
        address = f"{house_num} {street}, Freehold Township, NJ {zip_code}"
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
async def search(zip_code: str = "07728", min_years_owned: int = 30):
    """Original endpoint: Claude generates simulated property records."""
    client = claude_client()
    max_year = CURRENT_YEAR - min_years_owned

    prompt = f"""Generate 15 realistic property records for long-term homeowners in Freehold Township, NJ (zip: {zip_code}).
Each owner must have purchased in {max_year} or earlier (owned {min_years_owned}+ years).
Use real Freehold Township street names. Values must be realistic for Monmouth County NJ.

Return a JSON array of exactly 15 objects. Each object must have these exact keys:
address, owner_name, year_purchased, years_owned, assessed_value, estimated_current_value,
estimated_equity, equity_percentage, bedrooms, bathrooms, sq_ft, lot_size_acres, has_mortgage, tags

Rules:
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

@app.get("/api/real-search")
async def real_search(zip_code: str = "07728", min_years_owned: int = 30):
    """
    Returns real Monmouth County tax records for Freehold Township long-term owners.
    Falls back to realistic generated data if live scraping is unavailable.
    Caches results for 24 hours.
    """
    cache_path = DATA_DIR / f"cache_{zip_code}_{min_years_owned}.json"

    # Serve cache if fresh (< 24 hours)
    if cache_path.exists():
        age_hours = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600
        if age_hours < 24:
            cached = json.loads(cache_path.read_text())
            cached["cached"] = True
            return cached

    source = "realistic_fallback"
    properties: list[dict] = []
    scrape_note = ""

    # Attempt 1: live Monmouth County records
    max_purchase_year = CURRENT_YEAR - min_years_owned
    try:
        scraped = await scrape_monmouth_records(max_purchase_year=max_purchase_year)
        if len(scraped) >= 10:
            properties = scraped
            source = "monmouth_county_records"
        else:
            scrape_note = f"Scraper returned {len(scraped)} records (need ≥10); using fallback."
    except Exception as e:
        scrape_note = f"Scraper error: {e}; using fallback."

    # Fallback: deterministic realistic data
    if len(properties) < 10:
        properties = generate_fallback_properties(zip_code, min_years_owned, count=25)
        source = "realistic_fallback"

    # Filter to min_years_owned
    properties = [p for p in properties if p.get("year_purchased", 9999) <= max_purchase_year]

    result = {
        "source": source,
        "zip_code": zip_code,
        "min_years_owned": min_years_owned,
        "count": len(properties),
        "properties": properties,
        "cached": False,
        "notes": scrape_note or None,
    }

    cache_path.write_text(json.dumps(result, indent=2))
    return result


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

About Boomerville: A small, local group based in Freehold Township, NJ that connects with long-time homeowners
who may be open to exploring their options — direct sale, learning what their home is worth, or simply a conversation.

Letter requirements:
- Address them by first name ({first_name})
- Tone: warm friendly neighbor, NOT corporate, NOT pushy, NOT salesy
- Acknowledge the significance of {years_owned} years in one home
- Briefly mention Boomerville as a local buyer/researcher group (1-2 sentences)
- Make zero-obligation crystal clear — no pressure whatsoever
- Offer a free, no-strings conversation or home value chat
- Under 200 words total
- Sign off: "Warmly, The Boomerville Team — Freehold Township, NJ"
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
