import re
import os
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin

DOWNLOADS_DIR = os.path.join(os.path.dirname(__file__), "downloads")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
}


def _slug(name: str) -> str:
    return re.sub(r"[^\w-]", "_", name.lower())[:40]


def _save(url: str, path: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30, stream=True)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)
    return path


def _render_page(url: str, wait_ms: int = 6000) -> dict:
    """Headless-Playwright render of a JS-heavy model page (in a worker thread so
    it's safe to call from sync code). Returns {title, description, imgs[]}."""
    import threading
    import asyncio as _asyncio

    hold = {}

    async def _inner():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=HEADERS["User-Agent"],
                                            viewport={"width": 1400, "height": 1000})
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(wait_ms)
            for _ in range(3):  # trigger lazy-loaded gallery images
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)
            title = await page.title()
            desc = await page.evaluate(
                "() => { const m = document.querySelector('meta[property=\"og:description\"]')"
                " || document.querySelector('meta[name=\"description\"]'); return m ? m.content : ''; }"
            )
            imgs = await page.evaluate(
                "() => Array.from(document.querySelectorAll('img')).map(i => ({"
                " src: i.currentSrc || i.src || '', w: i.naturalWidth || 0, h: i.naturalHeight || 0 }))"
            )
            await browser.close()
            return {"title": title or "", "description": desc or "", "imgs": imgs}

    def _run():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            hold["data"] = loop.run_until_complete(_inner())
        except Exception as e:
            hold["error"] = str(e)
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=80)
    if "error" in hold:
        raise RuntimeError(hold["error"])
    return hold.get("data", {"title": "", "description": "", "imgs": []})


def _clean_title(title: str) -> str:
    for sep in [" by ", " | ", " - Thingiverse", " | Download", " - Free", " - 3D"]:
        if sep in title:
            title = title.split(sep)[0]
    return title.strip()


def _download_images(images: list[str], slug: str, limit: int = 12) -> list[str]:
    paths = []
    for i, img_url in enumerate(images[:limit]):
        ext = img_url.split("?")[0].split(".")[-1].lower()
        if not ext.isalpha() or len(ext) > 4:
            ext = "jpg"
        p = os.path.join(DOWNLOADS_DIR, f"{slug}_img_{i}.{ext}")
        try:
            _save(img_url, p)
            image_paths_ok = os.path.getsize(p) > 1024  # skip empty/broken
            if image_paths_ok:
                paths.append(p)
        except Exception:
            pass
    return paths


def _fetch_thingiverse(thing_id: str, slug: str) -> dict:
    from urllib.parse import urlparse, parse_qs, unquote
    data = _render_page(f"https://www.thingiverse.com/thing:{thing_id}")
    name = _clean_title(data["title"]) or f"Thing {thing_id}"
    slug = _slug(name)

    # Thingiverse serves images through resize.thingiverse.com/?url=<real cdn url>.
    # Unwrap to the real cdn.thingiverse.com asset and keep the largest per asset.
    best = {}
    for img in data["imgs"]:
        src = img["src"]
        real = src
        if "resize.thingiverse.com" in src and "url=" in src:
            q = parse_qs(urlparse(src).query)
            if q.get("url"):
                real = unquote(q["url"][0])
        if "cdn.thingiverse.com" not in real:
            continue
        key = re.sub(r"/(thumb|large|preview|featured|card|tiny)[^/]*", "", real)
        area = (img["w"] or 0) * (img["h"] or 0)
        if key not in best or area > best[key][0]:
            best[key] = (area, real)
    images = [s for _a, s in sorted(best.values(), key=lambda x: -x[0])]
    image_paths = _download_images(images, slug)

    # Public zip of all files (no login needed on Thingiverse)
    stl_path = os.path.join(DOWNLOADS_DIR, f"{slug}.zip")
    try:
        _save(f"https://www.thingiverse.com/thing:{thing_id}/zip", stl_path)
        if os.path.getsize(stl_path) < 1024:
            stl_path = None
    except Exception:
        stl_path = None

    return {"name": name, "description": data["description"][:500],
            "stl_path": stl_path, "image_paths": image_paths, "slug": slug}


def _fetch_printables(model_id: str, slug_hint: str) -> dict:
    data = _render_page(f"https://www.printables.com/model/{model_id}")
    name = _clean_title(data["title"]) or f"Model {model_id}"
    slug = _slug(name)

    # Printables gallery images live on media.printables.com under
    # /media/prints/<uuid>/... with several size variants per image. Group by the
    # print uuid and keep the largest variant of each.
    groups = {}
    for img in data["imgs"]:
        src = img["src"]
        if "media.printables.com" not in src:
            continue
        m = re.search(r"/(?:prints|models)/([0-9a-fA-F-]{16,})/", src)
        if not m:
            continue
        uuid = m.group(1)
        area = (img["w"] or 0) * (img["h"] or 0)
        if uuid not in groups or area > groups[uuid][0]:
            groups[uuid] = (area, src)
    images = [s for _a, s in sorted(groups.values(), key=lambda x: -x[0])]
    image_paths = _download_images(images, slug)

    # Printables gates model-file downloads behind login, like MakerWorld — the
    # uploader will fetch the file from the logged-in browser when available.
    return {"name": name, "description": data["description"][:500],
            "stl_path": None, "image_paths": image_paths, "slug": slug}


def _fetch_makerworld(model_id: str) -> dict:
    """Use Playwright in a background thread to fully render the MakerWorld page and scan every image."""
    import threading
    import asyncio as _asyncio

    result_holder = {}

    async def _scrape():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            page = await context.new_page()

            await page.goto(
                f"https://makerworld.com/en/models/{model_id}",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            # Let lazy-loaded images settle (networkidle never fires on MakerWorld
            # due to constant analytics traffic, so wait on a fixed delay instead)
            await page.wait_for_timeout(5000)
            # Scroll to trigger any lazy loading
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)

            # Page title → model name (strip MakerWorld's SEO suffixes)
            title = await page.title()
            for suffix in [" - Free 3D Print Model - MakerWorld", " - 3D Print Model - MakerWorld",
                           " - Free 3D Print Model", " - MakerWorld", " | MakerWorld"]:
                title = title.replace(suffix, "")
            name = title.strip() or f"Model {model_id}"

            # Meta description
            description = await page.evaluate("""
                () => {
                    const m = document.querySelector('meta[property="og:description"]')
                           || document.querySelector('meta[name="description"]');
                    return m ? m.content : '';
                }
            """) or ""

            # Collect EVERY <img> src on the page along with its natural size
            raw_imgs = await page.evaluate("""
                () => Array.from(document.querySelectorAll('img')).map(img => ({
                    src: img.currentSrc || img.src || '',
                    w: img.naturalWidth || img.width || 0,
                    h: img.naturalHeight || img.height || 0
                }))
            """)

            # The real product gallery images on MakerWorld live under the
            # ".../model/<code>/design/..." path. Everything else (avatars under
            # /user/ or /avatar/, review photos under /ratings/, comment photos
            # under /comment/, print-profile thumbnails under /instance/, and
            # store filament images on store.bblcdn.com) is page chrome we skip.
            #
            # The page ALSO embeds /design/ images for recommended/related models
            # (e.g. a different maker's design). Those belong to a *different*
            # model code, so we keep only images whose code matches THIS model's
            # gallery — i.e. the dominant code (the one with the most images).
            import re as _re
            from collections import Counter as _Counter

            design_imgs = []  # (code, key, area, src)
            code_counts = _Counter()
            for img in raw_imgs:
                src = img["src"]
                if not src.startswith("http") or "/design/" not in src:
                    continue
                m = _re.search(r"/model/([^/]+)/design/", src)
                code = m.group(1) if m else None
                if not code:
                    continue
                key = src.split("?")[0]  # path without the resize query
                area = (img["w"] or 0) * (img["h"] or 0)
                design_imgs.append((code, key, area, src))
                code_counts[code] += 1

            product_imgs = []
            if code_counts:
                main_code = code_counts.most_common(1)[0][0]
                # Dedupe the main model's images (1000x1000 vs 400x400 share a path)
                best = {}  # key -> (area, src)
                for code, key, area, src in design_imgs:
                    if code != main_code:
                        continue
                    if key not in best or area > best[key][0]:
                        best[key] = (area, src)
                # Download originals (strip OSS resize param for full resolution)
                product_imgs = [src.split("?")[0] for _area, src in best.values()]

            await browser.close()
            return {"name": name, "description": description[:500], "images": product_imgs}

    def _run():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            result_holder["data"] = loop.run_until_complete(_scrape())
        except Exception as e:
            result_holder["error"] = str(e)
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=60)

    if "error" in result_holder:
        raise RuntimeError(f"MakerWorld scrape failed: {result_holder['error']}")

    data = result_holder.get("data", {})
    name = data.get("name", f"Model {model_id}")
    slug = _slug(name)
    description = data.get("description", "")
    images = data.get("images", [])

    print(f"[makerworld] found {len(images)} images on page")

    image_paths = []
    for i, img_url in enumerate(images):
        ext = img_url.split(".")[-1].split("?")[0] or "jpg"
        if not ext.isalpha() or len(ext) > 4:
            ext = "jpg"
        p = os.path.join(DOWNLOADS_DIR, f"{slug}_img_{i}.{ext}")
        try:
            _save(img_url, p)
            image_paths.append(p)
        except Exception:
            pass

    # Attempt file download
    stl_path = None
    try:
        api_url = f"https://makerworld.com/api/v1/design-bom-file/download?designId={model_id}"
        r = requests.get(api_url, headers=HEADERS, timeout=15)
        if r.status_code == 200 and r.content[:2] == b"PK":
            stl_path = os.path.join(DOWNLOADS_DIR, f"{slug}.zip")
            with open(stl_path, "wb") as f:
                f.write(r.content)
    except Exception:
        pass

    return {"name": name, "description": description, "stl_path": stl_path, "image_paths": image_paths, "slug": slug}


def _fetch_generic(url: str) -> dict:
    """Try to download the URL directly as an STL file."""
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path) or "model"
    slug = _slug(os.path.splitext(filename)[0])
    stl_path = os.path.join(DOWNLOADS_DIR, f"{slug}.stl")
    try:
        _save(url, stl_path)
    except Exception:
        stl_path = None
    return {"name": filename, "description": "", "stl_path": stl_path, "image_paths": [], "slug": slug}


def fetch(url: str) -> dict:
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    # Thingiverse
    m = re.search(r"thingiverse\.com/thing:(\d+)", url)
    if m:
        return _fetch_thingiverse(m.group(1), m.group(1))

    # Printables
    m = re.search(r"printables\.com/model/(\d+)", url)
    if m:
        return _fetch_printables(m.group(1), m.group(1))

    # MakerWorld
    m = re.search(r"makerworld\.com/(?:\w+/)?models/(\d+)", url)
    if m:
        return _fetch_makerworld(m.group(1))

    # Generic direct URL
    return _fetch_generic(url)
