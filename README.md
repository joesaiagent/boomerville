# Boomerville

**Real estate intelligence platform for Monmouth County, NJ**

Boomerville helps local buyers find two types of motivated sellers:

1. **Long-term homeowners** — high-equity legacy owners who have owned for 20–40+ years and may be open to a direct sale
2. **Foreclosure leads** — homeowners with active Monmouth County Sheriff's sales, pulled live from court records

---

## Features

### Long-Term Homeowner Search
- Searches real Monmouth County tax assessor records
- Filters by years owned, township, and equity level
- Auto-tags: `High Equity`, `Legacy Owner`, `Long-term Owner`, `Free & Clear`
- Claude-powered outreach letter drafting
- Interactive Leaflet.js map with geocoded pins
- Save leads locally

### Foreclosure / Sheriff's Sales Tab
- Live data scraped from the Monmouth County Sheriff's Office (CivilView)
- Shows upset price (minimum bid), approx. judgment, potential equity
- Occupancy status, plaintiff/lender, attorney, court case #, parcel info
- Color-coded sale status: Scheduled · Adjournment · Bankrupt
- Days-until-sale countdown (color urgency: 🔴 ≤7d · 🟡 ≤30d · 🟢 30d+)
- AI-generated compassionate bail-out outreach letter per homeowner
- Save any foreclosure as a lead

### Covered Townships
| Township | County |
|---|---|
| Freehold Township | Monmouth |
| Howell Township | Monmouth |
| Wall Township | Monmouth |
| Millstone Township | Monmouth |

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI + Python 3.9+ |
| AI | Anthropic Claude API (letter drafting) |
| Data | Monmouth County tax assessor + CivilView sheriff's sales |
| Frontend | Vanilla HTML/CSS/JS (single file, no build step) |
| Maps | Leaflet.js + OpenStreetMap (no API key needed) |
| HTTP | httpx + BeautifulSoup4 |

---

## Quickstart

```bash
git clone https://github.com/joesaiagent/boomerville.git
cd boomerville

# Install deps
pip install fastapi uvicorn httpx beautifulsoup4 lxml aiofiles anthropic pydantic

# Set your Anthropic key (needed for letter drafting only)
export ANTHROPIC_API_KEY=sk-ant-...

# Start
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
# → open http://localhost:8000
```

Or use the included start script:
```bash
chmod +x start.sh && ./start.sh
```

---

## Data Sources

| Tab | Source | Refresh |
|---|---|---|
| Homeowner Search | Monmouth County Tax Assessor (`tax1.co.monmouth.nj.us`) | 24-hour cache |
| Foreclosure | Monmouth County Sheriff / CivilView (`salesweb.civilview.com`) | 6-hour cache |

Both scrapers fall back to realistic generated data if the live source is unavailable, so the app is always usable.

---

## Project Structure

```
boomerville/
├── backend/
│   └── main.py          # FastAPI app — all endpoints, scrapers, AI logic
├── frontend/
│   └── index.html       # Full UI — single file, no build step
├── data/                # Auto-created cache files (gitignored)
├── requirements.txt
└── start.sh
```

---

## Status

Active development. Currently local-only (no hosted deployment).

**Roadmap:**
- [ ] Skip trace integration (BatchSkipTracing / REI Skip) for owner phone/email
- [ ] Gmail outreach integration
- [ ] Expanded township coverage across Monmouth County
- [ ] Export leads to CSV
