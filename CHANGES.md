# CHANGES.md — Boomerville Build Summary

## What Was Built

This app was built from scratch as a full-stack local data platform targeting
long-term homeowners in Freehold Township, NJ (zip: 07728).

---

## Files Created

### `requirements.txt`
Python dependencies pinned for stability:
- `fastapi`, `uvicorn` — API server
- `anthropic` — Claude API for simulated search + letter drafting
- `httpx`, `beautifulsoup4`, `lxml` — HTTP + HTML scraping for Monmouth County records
- `pydantic` — request/response validation
- `aiofiles` — async file I/O

**Note:** Python 3.9 (system) is required. Python 3.14 (pyenv) breaks pydantic-core's
Rust build. A `.venv` using `/usr/bin/python3` (3.9.6) is provided.

---

### `backend/main.py`
Full FastAPI backend. Endpoints:

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Serves `frontend/index.html` |
| `/health` | GET | Server health check |
| `/api/search` | GET | Claude-simulated property records (demo mode) |
| `/api/real-search` | GET | Real data attempt → realistic fallback, 24h cache |
| `/api/draft-letter` | POST | Claude writes outreach letter for a property |
| `/api/save-lead` | POST | Saves property to `leads.json` (deduped by address) |
| `/api/leads` | GET | Returns all saved leads |
| `/api/leads/{address}` | DELETE | Removes a lead by address |

Key features:
- **Real NJ appreciation math**: era-specific annual rates (1970s–2020s) compound
  from purchase year to 2026 for accurate market value estimates
- **Equity calculation**: assessed value → market value (÷ 0.82 assessment ratio) →
  subtract mortgage balance estimate using 30-yr amortization
- **Tags auto-assigned**: "High Equity" ≥70%, "Legacy Owner" pre-1985, 
  "Long-term Owner" 30+ yrs, "Free & Clear" ≥95%
- **Monmouth County scraper** (`/api/real-search`): tries
  `tax1.co.monmouth.nj.us/cgi-bin/prc6.cgi` (district 0913 = Freehold Twp).
  Returns deterministic realistic fallback if scraper returns < 10 records.
- **24-hour cache**: results stored in `data/cache_<zip>_<years>.json`
- **Letter drafting**: warm, non-pressuring tone; first name only; no dollar amounts

---

### `frontend/index.html`
Single-file vanilla HTML/CSS/JS frontend. Features:

- **Dark UI** with gold/green accent palette
- **Search bar**: zip code + min years owned inputs
- **Data source toggle**: "Demo" (Claude-generated) vs "Real NJ Data" (real/fallback)
- **Stats bar**: count, avg equity, avg years owned, high equity count, legacy count
- **Source badge**: shows where data came from (live records / Claude / fallback / cached)
- **Results tab**: responsive card grid, equity progress bar, all tags
- **Map View tab**: Leaflet.js + OpenStreetMap (no API key needed). Geocodes addresses
  via Nominatim (rate-limited 1 req/300ms). Color-coded markers by tag.
- **Saved Leads tab**: persisted via `leads.json` on backend; remove button per card
- **Draft Letter button**: calls `/api/draft-letter`, shows letter in modal
- **Copy Letter**: copies to clipboard
- **Save Lead button**: calls `/api/save-lead`, deduped
- **API status indicator** in header (online/offline)
- **Toast notifications** for all actions

---

### `start.sh`
Convenience wrapper to start the server with helpful messaging if the API key is missing.

---

## Running the App

```bash
cd ~/Downloads/boomerville

# Set your API key first (needed for Draft Letter + Demo search)
export ANTHROPIC_API_KEY=sk-ant-...

# Start server
./start.sh

# Open in browser
open http://localhost:8000
```

Or run without the script:
```bash
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## What Requires a Paid API / Key

| Feature | Status | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | **Required for**: Demo search + Draft Letter | Free tier works; set in env before starting server |
| Monmouth County live scraper | Free (public records) | Currently returns 0 records — district/auth params may need tuning |
| Nominatim geocoding (map) | Free | Rate-limited; 25 properties ≈ 8 seconds to plot |
| Zillow Zestimate API | **Requires paid key** | Not implemented; NJ appreciation math used instead |
| Google Maps | Not used | Replaced with free Leaflet + OpenStreetMap |

---

## Test Results (2026-04-16)

```
GET /health                       → 200 ok
GET /api/real-search?zip=07728&min_years_owned=30  → 25 properties, source=realistic_fallback
GET /api/real-search (2nd call)   → 25 properties, cached=True
POST /api/save-lead               → status=saved, total_leads=1
GET /api/leads                    → 1 lead returned
POST /api/draft-letter            → requires ANTHROPIC_API_KEY
GET /api/search                   → requires ANTHROPIC_API_KEY
```

## Known Limitations / Next Steps

1. **Monmouth County scraper**: The PRC6 CGI endpoint may require different district codes
   or session auth. To improve: inspect network requests on
   `tax1.co.monmouth.nj.us` and update the `query_params` in `scrape_monmouth_records()`.

2. **NJ MOD-IV bulk download**: The statewide ZIP at
   `njgin.nj.gov/oit/gis/download/NJ_MOD-IV_Mod4_TaxList.zip`
   may work for batch import; add a one-time download+parse script if needed.

3. **ANTHROPIC_API_KEY**: Set `export ANTHROPIC_API_KEY=sk-ant-...` in your shell
   before running `./start.sh` to enable Claude features.

4. **Gmail integration**: `/api/draft-letter` generates the letter text. To actually
   send email, add a Gmail OAuth flow (requires Google Cloud project + credentials.json).

5. **Beds/baths from fallback**: The realistic fallback fills these with random values.
   Real scraped records would populate from the tax record's property class fields.
