"""
Discovery / research panel — lists the most popular models on a chosen site.
INFORMATION ONLY: it scrapes public listing pages and returns name, link,
thumbnail and stats. It does NOT download, edit, or upload anything.
"""
import asyncio
import random
import re

from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

# Per-site listing pages. Each site has SEVERAL popular/ranked listings; we pick
# one at random per request and then randomly sample from a large scrolled pool,
# so every click surfaces a different mix instead of the same top models.
SITES = {
    "makerworld": {
        # popularity-ranked listings only (no "newest")
        "urls": [
            "https://makerworld.com/en/3d-models?sort=trending",
            "https://makerworld.com/en/3d-models?sort=popular",
            "https://makerworld.com/en/3d-models?sort=hottest",
        ],
        "link_re": r"/models/(\d+)",
        "link_sel": "a[href*='/models/']",
    },
    "printables": {
        "urls": [
            "https://www.printables.com/model?ordering=-likes",
            "https://www.printables.com/model?ordering=-downloadCount",
            "https://www.printables.com/model?ordering=-rating",
        ],
        "link_re": r"/model/(\d+)",
        "link_sel": "a[href*='/model/']",
    },
    "thingiverse": {
        "urls": [
            "https://www.thingiverse.com/search?sort=popular&type=things",
            "https://www.thingiverse.com/search?sort=makes&type=things",
        ],
        "link_re": r"/thing:(\d+)",
        "link_sel": "a[href*='/thing:']",
    },
}


def _name_from_slug(url: str) -> str:
    # ".../models/2964241-gt-r-r34-mini-kit" -> "Gt R R34 Mini Kit"
    tail = url.rstrip("/").split("/")[-1]
    tail = re.sub(r"^(thing:)?\d+-?", "", tail, flags=re.I)   # strip leading id / thing:
    tail = re.sub(r"[-_]+", " ", tail).strip()
    return tail.title() if tail else ""


_NAV_NOISE = re.compile(r"popular|newest|most makes|filters|sort|trending|explore", re.I)


def _clean_name(slug_name: str, alt: str, title: str, card_text: str) -> str:
    # Prefer the URL slug name; then the image alt or link title; only use the
    # card text as a last resort, and reject obvious nav/filter-bar noise.
    for cand in (slug_name, alt, title):
        c = (cand or "").strip()
        c = re.sub(r"^(thumbnail representing|image of|photo of|preview of)\s+", "", c, flags=re.I)
        if c and not _NAV_NOISE.search(c):
            return c[:70]
    first = (card_text or "").split("  ")[0].strip()
    if first and not _NAV_NOISE.search(first):
        return first[:60]
    return "Untitled"


async def discover(site: str, count: int = 20) -> list[dict]:
    site = (site or "").lower()
    cfg = SITES.get(site)
    if not cfg:
        raise ValueError(f"Unknown site '{site}'. Choose from: {', '.join(SITES)}")

    # Pick a random ranked listing each call (trending / popular / newest / …) so
    # we don't keep landing on the exact same page.
    listing = random.choice(cfg["urls"])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 1000})
        page = await ctx.new_page()
        await page.goto(listing, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        # Scroll just enough to load the TOP of the (popularity-sorted) list — we
        # only sample from the most-popular slice, so no need to go deep.
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(800)

        items = await page.evaluate(
            """
            ({sel, linkRe}) => {
                const re = new RegExp(linkRe);
                const links = [...document.querySelectorAll(sel)];
                const seen = new Set(); const out = [];
                for (const a of links) {
                    const m = (a.href || '').match(re);
                    if (!m) continue;
                    const id = m[1];
                    if (seen.has(id)) continue; seen.add(id);
                    // climb just 2 levels to the immediate card (not the page chrome)
                    let card = a; for (let i=0;i<2 && card.parentElement;i++) card = card.parentElement;
                    const img = (a.querySelector('img') || card.querySelector('img'));
                    let thumb = '', alt = '';
                    if (img) {
                        alt = (img.alt || '').trim();
                        thumb = img.currentSrc || img.src || img.getAttribute('data-src') || '';
                        if (!thumb || thumb.startsWith('data:')) {
                            const ss = img.getAttribute('srcset') || img.getAttribute('data-srcset') || '';
                            if (ss) thumb = ss.split(',')[0].trim().split(' ')[0];
                        }
                    }
                    const title = (a.getAttribute('title') || a.getAttribute('aria-label') || '').trim();
                    const txt = (card.innerText || '').replace(/\\s+/g,' ').trim().slice(0, 140);
                    out.push({ id, url: a.href.split('#')[0].split('?')[0], thumb, alt, title, text: txt });
                }
                return out;
            }
            """,
            {"sel": cfg["link_sel"], "linkRe": cfg["link_re"]},
        )
        await browser.close()

    # The listing is sorted by popularity, so items[0..N] are the most popular.
    # Sample from just the TOP slice — this keeps results genuinely popular while
    # still varying which of the top models appear each time.
    top_pool = items[: max(count * 2, 30)]
    if len(top_pool) > count:
        items = random.sample(top_pool, count)
    else:
        items = top_pool
        random.shuffle(items)

    results = []
    for it in items:
        # try to lift a download/like count out of the card text (e.g. "12.3k")
        stat = ""
        mstat = re.search(r"(\d[\d.,]*\s*[kK]?)\s*(downloads?|likes?|prints?|boosts?|↓|♥)", it["text"])
        if mstat:
            stat = mstat.group(0)
        results.append({
            "name": _clean_name(_name_from_slug(it["url"]), it.get("alt", ""), it.get("title", ""), it["text"]),
            "url": it["url"],
            "thumb": it["thumb"],
            "stats": stat,
        })
    return results
