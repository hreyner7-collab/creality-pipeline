# Creality Pipeline

A Python / FastAPI pipeline that turns 3D-print model pages (Thingiverse, Printables, MakerWorld) into published **Creality Cloud** listings.

## What it does
1. **Discover** — research panel that lists the most popular models on a site (read-only; no download/upload).
2. **Fetch & Edit** — downloads a model's images and STL/CAD file, then recolors the product images to red with Gemini (`gemini-2.5-flash-image`).
3. **Publish** — drives your real Edge/Chrome (via CDP, `--remote-debugging-port=9222`) to fill the Creality Cloud upload form and submit.

## Setup
```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env        # then add your GOOGLE_API_KEY
python main.py              # serves the dashboard on http://localhost:8000
```

## Structure
- `main.py` — FastAPI app + dashboard routes
- `discover.py` — trending-model research scraper
- `downloader.py` — fetches images + model files from source sites
- `editor.py` — Gemini red-recolor of product images
- `uploader.py` — Creality Cloud upload via Edge CDP
- `creality/index.html` — dashboard UI

## Notes
- Your real `GOOGLE_API_KEY` lives in `.env` (gitignored) — never commit it.
- The upload step reuses your logged-in MakerWorld / Creality sessions from the Edge debug profile, so log in once in that window.
- Only repost models you have the rights to, with credit to the original designer.
