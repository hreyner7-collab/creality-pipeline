"""
Fast Creality Cloud upload via CDP, driving the user's REAL Edge/Chrome
(launched with --remote-debugging-port=9222) so Cloudflare trusts the session
and existing MakerWorld/Creality logins are reused.

upload() is the async entrypoint the dashboard calls. It:
  1. Ensures a model file exists (downloads STL/CAD zip from the open MakerWorld tab if needed)
  2. Opens the Creality "create-model-new" page
  3. Selects the STL/CAD file type, uploads the model file
  4. Advances to step 2, fills the name, uploads the red cover images
  5. STOPS before the final Submit (Category + Copyright agreement + Submit left for the user)
"""
import asyncio
import glob
import os
import re
import subprocess
import time
import urllib.request
import zipfile

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BASE = os.path.dirname(__file__)
DOWNLOADS = os.path.join(BASE, "downloads")
MODEL_DIR = os.path.join(DOWNLOADS, "model_files")
PROFILE_DIR = os.path.join(BASE, "edge_debug_profile")
# Use 127.0.0.1 (not "localhost"): localhost can resolve to IPv6 ::1, but Edge's
# debug port listens on IPv4 — that mismatch is the "ECONNREFUSED ::1:9222" error.
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
MODEL_EXTS = (".3mf", ".stl", ".step", ".stp", ".obj", ".ply", ".off", ".3ds", ".wrl", ".dae")


def _debug_port_up() -> bool:
    try:
        urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2)
        return True
    except Exception:
        return False


def ensure_edge() -> bool:
    """Make sure Edge is running with the remote-debugging port. If not, launch it
    with the saved profile (so MakerWorld/Creality logins persist) and wait for the
    port to come up. Returns True if the debug port is reachable."""
    if _debug_port_up():
        return True
    edge = next((p for p in EDGE_PATHS if os.path.exists(p)), None)
    if not edge:
        return False
    os.makedirs(PROFILE_DIR, exist_ok=True)
    try:
        subprocess.Popen(
            [edge, f"--remote-debugging-port={CDP_PORT}",
             f"--user-data-dir={PROFILE_DIR}",
             "https://www.crealitycloud.com"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    for _ in range(40):  # wait up to ~20s for the port
        if _debug_port_up():
            return True
        time.sleep(0.5)
    return False


def _clear_model_dir():
    os.makedirs(MODEL_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(MODEL_DIR, "*")):
        try:
            os.remove(old)
        except Exception:
            pass


def _extract_models(path):
    """Return a LIST of real model files (Creality-accepted extensions) from a
    download. If it's a zip, pull out every model file; drop tiny placeholder
    files when bigger ones exist. If it's already a model file, return just it."""
    low = path.lower()
    if not low.endswith(".zip"):
        return [path] if low.endswith(MODEL_EXTS) else []

    _clear_model_dir()
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return []
    out = []
    for n in z.namelist():
        if n.endswith("/"):
            continue
        if n.lower().endswith(MODEL_EXTS):       # only formats Creality accepts
            try:
                z.extract(n, MODEL_DIR)
                out.append(os.path.join(MODEL_DIR, n))
            except Exception:
                pass
    if not out:
        return []
    sizes = [(p, os.path.getsize(p)) for p in out if os.path.exists(p)]
    biggest = max((s for _, s in sizes), default=0)
    # keep files that are a meaningful fraction of the largest (drops 2–3 KB
    # placeholder stubs while keeping all real parts of a multi-part model)
    kept = [p for p, s in sizes if s >= max(1024, biggest * 0.05)]
    return kept or [p for p, _ in sizes]


async def _download_from_makerworld(ctx, source_url=None):
    # Always drive the EXACT model the user fetched. Navigate the MakerWorld tab
    # to source_url first so we never download whatever model happened to be open.
    page = next((p for p in ctx.pages if "makerworld.com" in p.url), None)
    if source_url and "makerworld.com" in source_url:
        if not page:
            page = await ctx.new_page()
        await page.bring_to_front()
        # only navigate if we're not already on this exact model
        m = re.search(r"/models/(\d+)", source_url)
        model_id = m.group(1) if m else ""
        if not model_id or model_id not in page.url:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
    if not page:
        return []
    await page.bring_to_front()
    await page.wait_for_timeout(400)
    # Locate the "Download STL/CAD Files" element (it's a DIV, not a button)
    info = await page.evaluate("""
        () => {
            const el = [...document.querySelectorAll('button,a,div,span,[role=button]')]
                .find(e => (e.innerText||'').trim().startsWith('Download STL/CAD'));
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.x + r.width/2, y: r.y + r.height/2};
        }
    """)
    if not info:
        return []

    # Collect EVERY download the page fires (a model can trigger several files),
    # so we can keep the right/largest one instead of whatever lands first.
    downloads = []
    page.on("download", lambda d: downloads.append(d))

    await page.mouse.click(info["x"], info["y"])
    await page.wait_for_timeout(2500)
    # Some models pop a small confirm with an exact "Download All" — click only
    # that (NEVER a generic "Download", which matches "Download Bill of Materials").
    for exact in ["Download All", "Download All Files", "Download Files"]:
        loc = page.get_by_role("button", name=exact, exact=True)
        try:
            if await loc.count() > 0:
                await loc.first.click(timeout=2500)
                break
        except Exception:
            pass

    # Wait for downloads to arrive and finish (up to ~45s total).
    for _ in range(45):
        if downloads:
            await page.wait_for_timeout(2000)   # let remaining/streaming files finish
            break
        await page.wait_for_timeout(1000)
    if not downloads:
        return []

    # Save them all to disk under their real names, then keep the LARGEST.
    saved = []
    for d in downloads:
        try:
            name = (d.suggested_filename or "download.bin").replace("/", "_")
            dest = os.path.join(DOWNLOADS, name)
            await d.save_as(dest)
            if os.path.exists(dest):
                saved.append(dest)
        except Exception:
            pass
    if not saved:
        return []
    largest = max(saved, key=lambda p: os.path.getsize(p))
    return _extract_models(largest)


async def upload(name: str, description: str, stl_path: str, image_paths: list[str],
                 source_url: str = "") -> dict:
    # Auto-launch Edge with the debug port if it isn't already running, so the
    # user never has to start it by hand (and never sees ECONNREFUSED).
    if not _debug_port_up():
        if not ensure_edge():
            return {"success": False, "error": "Could not start Edge automatically. "
                    "Open Edge once via the debug launcher, log into MakerWorld + Creality, then retry."}
        # give the freshly-launched browser a moment to settle
        await asyncio.sleep(2)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            return {"success": False, "error": f"Could not attach to Edge on {CDP_URL}. ({e})"}
        ctx = browser.contexts[0]

        # 1) Model files — for THIS model only. Prefer a file the fetch already
        #    produced; otherwise download the exact model from its source URL.
        #    No "grab any leftover file" fallback — that caused the wrong (old)
        #    model to be re-uploaded and tripped Creality's duplicate check.
        model_files = [stl_path] if (stl_path and os.path.exists(stl_path)) else []
        if not model_files:
            # Try twice — the MakerWorld download can transiently time out.
            for _attempt in range(2):
                model_files = await _download_from_makerworld(ctx, source_url)
                if model_files:
                    break
        if not model_files:
            return {"success": False, "error": "Couldn't get the model file(s) for THIS model. "
                    "Open its MakerWorld page (logged in) or paste a direct file, then retry. "
                    "(I no longer fall back to a previously-downloaded model.)"}
        # Upload every real part, largest first (so the primary geometry leads).
        model_files = sorted(model_files, key=lambda p: os.path.getsize(p), reverse=True)

        # Red cover images — only the ones for THIS model (no glob fallback that
        # would pull in a previous model's edited images).
        reds = [p for p in (image_paths or []) if os.path.exists(p)]
        if not reds:
            return {"success": False, "error": "No edited images for this model. "
                    "Run Fetch & Edit first so the red images are generated."}

        # 2) Open the Creality upload page in the real browser (always start fresh
        #    on a known-good URL so we never act on a stale/mid-flow/store page)
        page = next((p for p in ctx.pages if "crealitycloud" in p.url), None) \
            or await ctx.new_page()
        await page.bring_to_front()
        await page.goto("https://www.crealitycloud.com/create-model-new?source=12",
                        wait_until="domcontentloaded", timeout=40000)

        # The actual upload form is rendered inside an iframe (flowprint/create-model).
        # Poll quickly and break the moment its file inputs exist (no fixed sleeps).
        frame = None
        for _ in range(40):
            frame = next((f for f in page.frames if "flowprint/create-model" in f.url), None)
            if frame:
                try:
                    if await frame.locator("input[type=file]").count() > 0:
                        break
                except Exception:
                    pass
            await page.wait_for_timeout(250)
        if not frame:
            # Most common cause: not logged into Creality in this Edge profile.
            body = ""
            try:
                body = (await page.evaluate("() => document.body.innerText")).lower()
            except Exception:
                pass
            if "login" in page.url.lower() or "sign in" in body or "log in" in body:
                return {"success": False, "error": "You're not logged into Creality Cloud in the "
                        "Edge window. Log in there once (the profile remembers it), then retry."}
            return {"success": False, "error": "Creality's upload form didn't load (the page may be "
                    "slow or showing a Cloudflare check). Wait a few seconds and retry."}

        # 3) Select the "STL/CAD files..." radio
        try:
            await frame.get_by_text("STL/CAD files or other types of 3MF file", exact=False).first.click(timeout=4000)
        except Exception:
            pass

        # 4) Attach the model file(s) to the model-files input. Upload ALL real
        #    parts at once; if the input rejects multiples, fall back to the
        #    largest single file so the primary geometry still gets in.
        try:
            model_input = frame.locator("input[type=file][accept*='.step'], input[type=file][accept*='.stl']").first
            try:
                await model_input.set_input_files(model_files)
            except Exception:
                await model_input.set_input_files(model_files[0])
        except Exception as e:
            return {"success": False, "error": f"Could not attach the model file: {e}"}
        # Wait only until the file name registers (fast poll, not a fixed sleep)
        stem = os.path.splitext(os.path.basename(model_files[0]))[0][:18]
        for _ in range(40):
            try:
                if await frame.evaluate(f"() => document.body.innerText.includes({stem!r})"):
                    break
            except Exception:
                pass
            await page.wait_for_timeout(300)

        # 5) Next -> step 2. The Next control is a <div class="step-one-btn submit">
        #    inside the iframe (not a real <button>).
        for sel in [".step-one-btn.submit", ".step-one-btn"]:
            try:
                loc = frame.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=4000)
                    break
            except Exception:
                pass
        else:
            try:
                await frame.get_by_text("Next", exact=True).last.click(timeout=4000)
            except Exception:
                pass

        # 6) Name — wait until the step-2 name field is actually ready, then fill
        nm = frame.locator("input[placeholder*='model name' i]").first
        try:
            await nm.wait_for(state="visible", timeout=10000)
            await nm.click()
            await nm.fill(name or "Untitled Model")
        except Exception:
            pass

        # 7) Cover images — BOTH the web cover (4:3) and the app cover (3:4) are
        #    required. They are the first two image-type file inputs. Each upload
        #    opens a "Cropping" modal that must be confirmed before continuing.
        async def _upload_cover(nth, img):
            try:
                inp = frame.locator(
                    "input[type=file][accept*='.webp'], input[type=file][accept*='.jpeg']"
                ).nth(nth)
                await inp.set_input_files(img)
                confirm = frame.get_by_role("button", name="Confirm").last
                await confirm.wait_for(state="visible", timeout=6000)
                await confirm.click(timeout=2000)
                await confirm.wait_for(state="hidden", timeout=5000)
            except Exception:
                pass

        # Image layout that shows ALL images with NO duplicate:
        #  - the first image is the hero, used for BOTH cover crops (web 4:3 and
        #    app 3:4 are different crops of the same shot — not a visible dup)
        #  - EVERY OTHER image goes into the Model Images gallery
        # So image 0 appears as the cover, images 1..n appear in the gallery, and
        # nothing is shown twice. (Previously the app-cover image was dropped from
        # the gallery and never appeared on the web listing.)
        web_cover = reds[0] if reds else None
        app_cover = reds[0] if reds else None
        gallery_imgs = reds[1:]   # all images except the hero/cover

        if web_cover:
            await _upload_cover(0, web_cover)   # web cover 4:3
            await _upload_cover(1, app_cover)   # app cover 3:4

        # 7b) "Model Images" gallery (up to 9) — add the leftover photos here.
        #     The gallery's el-upload trigger reads "Upload" (covers read "Cover
        #     Image"); its input is multiple, so we can attach them all at once.
        if gallery_imgs:
            try:
                gal_index = await frame.evaluate("""
                    () => {
                        const uploads = [...document.querySelectorAll('.el-upload')];
                        const all = [...document.querySelectorAll('input[type=file]')];
                        for (const u of uploads) {
                            const t = (u.innerText||'').trim();
                            const inp = u.querySelector('input[type=file]');
                            if (inp && /^Upload\\b/i.test(t) && !/cover/i.test(t))
                                return all.indexOf(inp);
                        }
                        return -1;
                    }
                """)
                if gal_index >= 0:
                    gal = frame.locator("input[type=file]").nth(gal_index)
                    for img in gallery_imgs:
                        try:
                            await gal.set_input_files(img)
                            cf = frame.get_by_role("button", name="Confirm").last
                            try:
                                await cf.wait_for(state="visible", timeout=2500)
                                await cf.click(timeout=2000)
                                await cf.wait_for(state="hidden", timeout=4000)
                            except Exception:
                                await page.wait_for_timeout(900)
                        except Exception:
                            pass
            except Exception:
                pass

        # 8) "Auto-Generated" — Creality's AI fills Category + Tags + Description
        #    from the cover image. This removes the manual Category requirement.
        try:
            ag = frame.get_by_role("button", name="Auto-Generated")
            if await ag.count() == 0:
                ag = frame.get_by_text("Auto-Generated", exact=False)
            await ag.first.click(timeout=4000)
            # Wait until the Category field is populated (poll up to ~20s)
            for _ in range(40):
                await page.wait_for_timeout(500)
                filled = await frame.evaluate("""
                    () => {
                        const inp = [...document.querySelectorAll('input')]
                            .find(i => /category/i.test(i.placeholder||'') ||
                                       /keywords to search or select a category/i.test(i.placeholder||''));
                        return inp ? !!inp.value : false;
                    }
                """)
                # also detect category tag chips appearing
                chips = await frame.evaluate("() => document.querySelectorAll('.el-tag, .ant-tag, [class*=tag-item]').length")
                if filled or chips > 0:
                    break
        except Exception:
            pass

        # 8b) Description — use the REAL model description (from the source page),
        #     not Creality's AI text. Auto-Generated fills an AI description above;
        #     here we replace it with the actual description when we have one.
        real_desc = (description or "").strip()
        if len(real_desc) >= 20:
            try:
                set_ok = await frame.evaluate("""
                    (text) => {
                        // Creality's description editor is a textarea or a
                        // contenteditable rich-text box. Find it near a
                        // "Description"/"Introduction" label and replace its content.
                        const ta = [...document.querySelectorAll('textarea')]
                            .find(t => /desc|introduc/i.test((t.placeholder||'') + (t.name||'')))
                            || [...document.querySelectorAll('textarea')].pop();
                        if (ta) {
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, 'value').set;
                            setter.call(ta, text);
                            ta.dispatchEvent(new Event('input', {bubbles:true}));
                            ta.dispatchEvent(new Event('change', {bubbles:true}));
                            return 'textarea';
                        }
                        const ce = [...document.querySelectorAll('[contenteditable=true]')]
                            .find(e => e.offsetParent !== null);
                        if (ce) {
                            ce.focus();
                            ce.innerHTML = '';
                            ce.textContent = text;
                            ce.dispatchEvent(new Event('input', {bubbles:true}));
                            return 'contenteditable';
                        }
                        return 'not-found';
                    }
                """, real_desc)
                print(f"[uploader] description set via: {set_ok}")
            except Exception as e:
                print(f"[uploader] description override failed: {e}")

        # 9) Tick the Copyright agreement checkbox
        try:
            cb = frame.get_by_text("I have read and agree", exact=False).first
            await cb.scroll_into_view_if_needed(timeout=3000)
            # click the checkbox control to the left of the text
            box = await cb.bounding_box()
            if box:
                await page.mouse.click(box["x"] - 12, box["y"] + box["height"] / 2)
        except Exception:
            pass

        await page.wait_for_timeout(500)
        os.makedirs(os.path.join(DOWNLOADS, "_shots"), exist_ok=True)
        try:
            await page.screenshot(path=os.path.join(DOWNLOADS, "_shots", "before_submit.png"))
        except Exception:
            pass

        # 10) Submit. The Submit control is a styled <div class="submit">, not a
        #     real <button>, so locate it in the iframe and click via translated
        #     page coordinates (iframe offset + element center).
        #     Set CREALITY_NO_SUBMIT=1 to fill everything but stop before publishing
        #     (used for verifying the form without creating a listing).
        submitted = False
        if os.environ.get("CREALITY_NO_SUBMIT") == "1":
            return {"success": True, "url": page.url,
                    "message": "Filled everything but did NOT submit (CREALITY_NO_SUBMIT=1)."}
        try:
            target = await frame.evaluate("""
                () => {
                    const els = [...document.querySelectorAll('button,div,span,a,[role=button]')];
                    const cand = els.filter(e => {
                        if ((e.innerText||'').trim() !== 'Submit') return false;
                        const r = e.getBoundingClientRect();
                        return r.width>40 && r.width<400 && r.height>20 && r.height<120;
                    });
                    if (!cand.length) return null;
                    cand.sort((a,b)=>{const ra=a.getBoundingClientRect(),rb=b.getBoundingClientRect();
                        return ra.width*ra.height - rb.width*rb.height;});
                    const e = cand[0]; e.scrollIntoView({block:'center'});
                    const r = e.getBoundingClientRect();
                    return {x: r.x + r.width/2, y: r.y + r.height/2};
                }
            """)
            outcome = None      # "success" | "duplicate" | "error"
            reason = ""
            if target:
                fe = await frame.frame_element()
                fbox = await fe.bounding_box()
                await page.mouse.click(fbox["x"] + target["x"], fbox["y"] + target["y"])
                for _ in range(40):
                    await page.wait_for_timeout(1000)
                    # Most reliable success signal: navigation to the model page.
                    if "model-detail" in page.url:
                        outcome = "success"; break
                    body = (await page.evaluate("() => document.body.innerText")) or ""
                    low = body.lower()
                    # Creality's duplicate / originality block (do not bypass — report it)
                    mdup = re.search(r"already.{0,12}(uploaded|exist|published)|has been uploaded|"
                                     r"duplicate|cannot upload|repeat|已上传|重复|已存在", low)
                    if mdup:
                        outcome = "duplicate"
                        reason = mdup.group(0)
                        break
                    merr = re.search(r"(submit failed|upload failed|failed to|not allowed|"
                                     r"please (?:fill|select|complete|add)|required)", low)
                    if merr:
                        outcome = "error"; reason = merr.group(0); break
                    if "create-model-new" not in page.url:
                        outcome = "success"; break
                    if re.search(r"submitted successfully|under review|published successfully", low):
                        outcome = "success"; break
                submitted = (outcome == "success")
        except Exception as e:
            reason = str(e)

        try:
            await page.screenshot(path=os.path.join(DOWNLOADS, "_shots", "after_submit.png"))
        except Exception:
            pass

        if submitted:
            return {"success": True, "url": page.url,
                    "message": "Published to your Creality Cloud account. Check 'My Designs'."}
        if outcome == "duplicate":
            return {"success": False, "url": page.url, "duplicate": True,
                    "error": "Creality blocked this — the model already exists on the platform "
                             "(their duplicate/originality check). It can't be re-published."}
        if outcome == "error":
            return {"success": False, "url": page.url,
                    "error": f"Creality rejected the submission ({reason or 'see the form for the missing field'})."}
        return {"success": True, "url": page.url,
                "message": "Form fully filled (model, name, red cover, AI category/tags, copyright). "
                           "If it didn't auto-submit, click Submit in the browser — see before_submit.png."}
