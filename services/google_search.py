"""Google search via headless Playwright, used to find a company's official website.

Plain HTTP gets a JavaScript wall from Google, and DuckDuckGo's results for Taiwanese
companies are dominated by registry mirrors, so a real browser is the only way to see
what a human sees. Google rate-limits automated traffic with a CAPTCHA page
(/sorry/); when that happens we back off for a cooldown instead of hammering it, and
callers fall back to DuckDuckGo.
"""
import asyncio
import logging
import time
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
_COOLDOWN_SECONDS = 600
_blocked_until = 0.0
_lock = asyncio.Lock()  # one browser at a time; keeps request rate human-like

_EXTRACT_JS = """() => [...document.querySelectorAll('a h3')].map(h => {
  const a = h.closest('a');
  const box = a.closest('div.g') || a.parentElement.parentElement;
  return {href: a.href, title: h.innerText, snippet: box ? box.innerText : ''};
})"""


def is_available() -> bool:
    return time.monotonic() >= _blocked_until


async def _resolve(client: httpx.AsyncClient, href: str) -> str | None:
    """Google wraps result links in /goto?url=...; the 302 Location is the real URL."""
    if "google.com/goto" not in href:
        return href if href.startswith("http") else None
    try:
        resp = await client.get(href, follow_redirects=False)
    except httpx.HTTPError:
        return None
    loc = resp.headers.get("location", "")
    return loc if resp.is_redirect and loc.startswith("http") else None


async def search_google(query: str, *, limit: int = 10) -> list[dict[str, str]]:
    """Return [{url, title, snippet}] in Google's order; [] when blocked or unavailable."""
    global _blocked_until
    if not is_available():
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return []
    async with _lock:
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True, args=["--disable-blink-features=AutomationControlled"])
                try:
                    ctx = await browser.new_context(locale="zh-TW", user_agent=_UA)
                    page = await ctx.new_page()
                    await page.goto(
                        f"https://www.google.com/search?hl=zh-TW&num={limit}&q={quote(query)}",
                        wait_until="domcontentloaded", timeout=20000)
                    if "/sorry/" in page.url:
                        _blocked_until = time.monotonic() + _COOLDOWN_SECONDS
                        log.warning("Google CAPTCHA; pausing Google search for %ss", _COOLDOWN_SECONDS)
                        return []
                    raw = await page.evaluate(_EXTRACT_JS)
                finally:
                    await browser.close()
        except Exception:
            log.warning("Google search via Playwright failed", exc_info=True)
            return []
    results = []
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": _UA}) as client:
        for item in raw[:limit]:
            url = await _resolve(client, item["href"])
            if url:
                results.append({"url": url, "title": item["title"].strip(),
                                "snippet": item["snippet"].strip()})
    return results
