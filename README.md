# Creality Pipeline

A Python / FastAPI pipeline that turns 3D-print model pages (Thingiverse, Printables, MakerWorld) into published **Creality Cloud** listings.

## What it does
1. **Discover** — research panel that lists the most popular models on a site (read-only; no download/upload).
2. **Fetch & Edit** — downloads a model's images and STL/CAD file, then recolors the product images to red with Gemini (`gemini-2.5-flash-image`).
3. **Publish** — drives your real Edge/Chrome (via CDP, `--remote-debugging-port=9222`) to fill the Creality Cloud upload form and submit.

## What you MUST provide before it will run
This is **not** a zero-setup app. You need to supply these yourself (they are never stored in this repo):

1. **Python 3.10+** installed on your machine.
2. **A Google Gemini API key** (`GOOGLE_API_KEY`) — used by `editor.py` to recolor images. Get one free at https://aistudio.google.com/apikey
3. **Microsoft Edge** browser installed (the upload step drives your real Edge via CDP).
4. **One-time logins** in that Edge window to **MakerWorld** and **Creality Cloud** (so the uploader can reuse your sessions).
5. **Playwright's Chromium** browser (downloaded below).

## Step-by-step setup
```bash
# 1. Clone the repo
git clone https://github.com/hreyner7-collab/creality-pipeline.git
cd creality-pipeline

# 2. Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate        # Windows  (use: source .venv/bin/activate on macOS/Linux)

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install the Playwright Chromium browser
playwright install chromium

# 5. Add your API key
cp .env.example .env
#    then open .env and set:  GOOGLE_API_KEY=your_real_key_here

# 6. Log in once (in Edge) to MakerWorld and Creality Cloud
#    The uploader reuses these sessions from the Edge debug profile.

# 7. Run the dashboard
python main.py
#    open http://localhost:8000 in your browser
```

## How to use the dashboard
1. **Find Top Models** (optional research) — pick a site, click *Find*, browse trending models.
2. **Model URL** — paste a Thingiverse / Printables / MakerWorld URL, click *Fetch & Edit* (downloads files + recolors images with Gemini).
3. **Publish to Creality Cloud** — click *Upload to Creality Cloud* (drives Edge, fills the form; log in if prompted, then confirm the final Submit).

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
