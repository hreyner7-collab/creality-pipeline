import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from downloader import fetch as dl_fetch
from editor import edit_all
from uploader import upload as uploader_upload
from discover import discover as discover_models

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"

app = FastAPI(title="Creality Pipeline")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)


# ── Request / Response models ─────────────────────────────────────────────────

class FetchRequest(BaseModel):
    url: str

class UploadRequest(BaseModel):
    name: str
    description: str
    stl_path: str
    image_paths: list[str]
    source_url: str = ""

class DiscoverRequest(BaseModel):
    site: str
    count: int = 20


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html = (BASE_DIR / "creality" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/fetch")
async def fetch_and_edit(req: FetchRequest):
    try:
        result = dl_fetch(req.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")

    # Run Gemini image editing on all preview images
    edited_paths = []
    if result.get("image_paths"):
        try:
            edited_paths = edit_all(result["image_paths"])
        except Exception as e:
            # Don't fail the whole request if editing errors — surface it as warning
            edited_paths = []
            result["edit_warning"] = str(e)

    result["edited_paths"] = edited_paths

    # Convert paths to URL-safe /file/ references for the frontend
    def to_url(p):
        if p:
            return "/file/" + Path(p).name
        return None

    return {
        "name": result.get("name", ""),
        "description": result.get("description", ""),
        "source_url": req.url,
        "stl_path": result.get("stl_path"),
        "stl_url": to_url(result.get("stl_path")),
        "original_images": [to_url(p) for p in result.get("image_paths", [])],
        "edited_images": [to_url(p) for p in edited_paths],
        "edit_warning": result.get("edit_warning"),
        "_raw_stl": result.get("stl_path"),
        "_raw_images": edited_paths if edited_paths else result.get("image_paths", []),
    }


@app.post("/discover")
async def discover_endpoint(req: DiscoverRequest):
    """Research only: list the top/most-popular models on a site. Does not
    download, edit, or upload anything."""
    try:
        models = await discover_models(req.site, max(1, min(req.count, 40)))
        return {"site": req.site, "models": models}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Discovery failed: {e}")


@app.post("/upload")
async def upload_to_creality(req: UploadRequest):
    try:
        result = await uploader_upload(req.name, req.description, req.stl_path,
                                       req.image_paths, req.source_url)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/file/{filename}")
async def serve_file(filename: str):
    # Security: only serve files from the downloads directory
    safe_path = DOWNLOADS_DIR / Path(filename).name
    if not safe_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(safe_path))
