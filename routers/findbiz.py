"""
findbiz.py — 透過 Playwright 手動通過 Cloudflare，抓取 findbiz.nat.gov.tw 的每股金額。

流程：
  POST /api/findbiz/scrape   → 啟動 Playwright browser，回傳 session_id
  GET  /api/findbiz/stream/{session_id}  → SSE 進度推送
  POST /api/findbiz/confirm/{session_id} → 使用者通知「已通過 Cloudflare」
"""
import asyncio
from services.task_progress import spawn_background as _spawn
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services import data_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/findbiz", tags=["findbiz"])

FINDBIZ_INIT = "https://findbiz.nat.gov.tw/"
# persistent browser profile 路徑，用來儲存 cf_clearance 等 Cloudflare cookie
FINDBIZ_PROFILE_DIR = str(Path(__file__).parent.parent / "data" / "findbiz_profile")


def _resolve_display() -> str | None:
    """
    WSL2 systemd service 不繼承使用者的 DISPLAY 環境變數。
    依序嘗試幾個來源，找到就設進 os.environ 並回傳。
    """
    if os.environ.get("DISPLAY"):
        return os.environ["DISPLAY"]

    # WSLg 幾乎都是 :0，socket 存在就採用
    if os.path.exists("/tmp/.X11-unix/X0"):
        os.environ["DISPLAY"] = ":0"
        return ":0"

    # 從 init(pid=1) 或父 process 的 /proc/*/environ 裡找
    for pid in [1, os.getppid()]:
        try:
            env_raw = open(f"/proc/{pid}/environ", "rb").read().decode("utf-8", errors="replace")
            for token in env_raw.split("\0"):
                if token.startswith("DISPLAY="):
                    val = token[8:]
                    os.environ["DISPLAY"] = val
                    return val
        except Exception:
            pass

    return None
FINDBIZ_LIST = "https://findbiz.nat.gov.tw/fts/query/QueryList/queryList.do"
FINDBIZ_BASE = "https://findbiz.nat.gov.tw"

# session_id -> {queue, event, done, company_id, tax_id}
_sessions: dict[str, dict] = {}


class _CloudflareChallenge(RuntimeError):
    """findbiz returned a Cloudflare challenge instead of the requested page."""


class ScrapeRequest(BaseModel):
    company_id: str
    tax_id: str


def _parse_int(s: str) -> int:
    if not s:
        return 0
    return int(re.sub(r"[^\d]", "", s) or "0")


def _is_no_par_value(value: str) -> bool:
    """FindBiz 的「無票面金額」是有效狀態，不是數值缺漏。"""
    return "無票面金額" in (value or "").replace(" ", "")


def _is_cloudflare_challenge(html: str) -> bool:
    """Recognize Cloudflare interstitials without confusing them with no results."""
    sample = (html or "").lower()
    return any(marker in sample for marker in (
        "<title>just a moment",
        "<title>請稍候",
        "window._cf_chl_opt",
    ))


async def _wait_for_cloudflare(page, queue: asyncio.Queue, event: asyncio.Event) -> bool:
    """Wait until the visible Playwright page has actually left the challenge.

    A cf_clearance cookie can exist while already expired or otherwise rejected,
    so the page content—not cookie presence—is the source of truth.
    """
    await queue.put({
        "type": "browser_ready",
        "message": (
            "findbiz 的 Cloudflare 驗證已失效。請在剛開啟的 Chromium 完成"
            "「驗證您是真人」；通過後系統會自動繼續。"
        ),
    })
    for _ in range(60):
        try:
            if not _is_cloudflare_challenge(await page.content()):
                return True
            if event.is_set():
                event.clear()
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                if not _is_cloudflare_challenge(await page.content()):
                    return True
        except Exception as exc:
            log.debug("findbiz: waiting for Cloudflare page: %s", exc)
        await asyncio.sleep(2)
    return False


def _parse_detail_html(html: str) -> dict:
    """
    從 findbiz detail page HTML 解析「公司基本資料」表格的 key-value。

    歷史：早期版本只解析 `<div id="tabCmpyContent">` 內的 table，但 findbiz
    改版後容器 id 不再固定，且舊解析器一遇到 cell 內的巢狀 <div> 就會提早結束。
    現在改成全頁掃 <tr>：凡是有 ≥2 個儲存格的列，就以第一格為 key、第二格為 value。
    我們只關心 每股金額(元) / 已發行股份總數(股) / 實收資本額(元) 這幾個唯一標籤，
    不會跟董監事名單等其他 table 的列衝突。
    """
    from html.parser import HTMLParser

    class _TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.cells: list[str] = []
            self._cur  = ""
            self._in_cell = False
            self.result: dict[str, str] = {}

        def handle_starttag(self, tag, attrs):
            if tag in ("td", "th"):
                self._in_cell = True
                self._cur     = ""
            elif tag == "tr":
                self.cells = []

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self._in_cell:
                self.cells.append(self._cur.strip())
                self._in_cell = False
            elif tag == "tr" and len(self.cells) >= 2:
                key, val = self.cells[0], self.cells[1]
                # 後出現的不覆蓋先出現的（保留主資料表的值）
                if key and key not in self.result:
                    self.result[key] = val

        def handle_data(self, data):
            if self._in_cell:
                self._cur += data

        def handle_entityref(self, name):
            import html as h
            if self._in_cell:
                self._cur += h.unescape(f"&{name};")

        def handle_charref(self, name):
            import html as h
            if self._in_cell:
                self._cur += h.unescape(f"&#{name};")

    parser = _TableParser()
    parser.feed(html)
    return parser.result


async def _parse_detail_page(page) -> dict:
    """Playwright DOM 版（備用）。"""
    result = {}
    rows = await page.query_selector_all("#tabCmpyContent tbody tr")
    for row in rows:
        tds = await row.query_selector_all("td")
        if len(tds) < 2:
            continue
        key   = (await tds[0].inner_text()).strip()
        value = (await tds[1].inner_text()).strip()
        if key:
            result[key] = value
    return result


async def _search_and_load_detail(page, tax_id: str) -> str | None:
    """
    優先直接開新版 `/fts/company/<統編>` 詳細頁；若站方不接受直接 URL，
    再退回 POST 搜尋 → 點結果的舊流程。
    """
    # 新版 findbiz 的公司頁已有穩定直連。這條 fast path 少一次 POST、一次
    # navigation 與固定 2 秒等待，正常 session 下可直接取得詳細頁。
    direct_url = f"{FINDBIZ_BASE}/fts/company/{tax_id}"
    try:
        await page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
        direct_html = await page.content()
        if _is_cloudflare_challenge(direct_html):
            log.warning("findbiz: Cloudflare challenge on direct detail URL for %s", tax_id)
            raise _CloudflareChallenge
        if tax_id in direct_html and ("每股金額" in direct_html or "已發行股份總數" in direct_html):
            return direct_html
    except _CloudflareChallenge:
        raise
    except Exception as exc:
        log.info("findbiz direct detail unavailable for %s, falling back to search: %s", tax_id, exc)

    # 舊站相容：提交搜尋，再從結果頁開公司詳細資料。
    params = {
        "errorMsg": "", "validatorOpen": "N", "rlPermit": "0",
        "userResp": "", "curPage": "0", "fhl": "zh_TW",
        "qryCond": tax_id, "infoType": "D",
        "qryType": "cmpyType", "cmpyType": "true",
        "brCmpyType": "", "busmType": "", "factType": "",
        "lmtdType": "", "isAlive": "all",
        "busiItemMain": "", "busiItemSub": "",
    }
    # Step 1: 提交搜尋
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
            await page.evaluate(
                """([url, params]) => {
                    const form = document.createElement('form');
                    form.method = 'POST';
                    form.action = url;
                    for (const [k, v] of Object.entries(params)) {
                        const inp = document.createElement('input');
                        inp.type = 'hidden'; inp.name = k; inp.value = v;
                        form.appendChild(inp);
                    }
                    document.body.appendChild(form);
                    form.submit();
                }""",
                [FINDBIZ_LIST, params],
            )
        await asyncio.sleep(2)
        search_html = await page.content()
    except Exception as exc:
        log.error("findbiz search form submit failed: %s", exc)
        return None

    if _is_cloudflare_challenge(search_html):
        log.warning("findbiz: still cloudflare after search. HTML[:200]=%s", search_html[:200])
        raise _CloudflareChallenge

    # Step 2: 點擊第一個公司連結
    # findbiz 改版後，搜尋結果的公司連結改成乾淨 URL `/fts/company/<統編>`，
    # 不再是含 `queryCmpyDetail` 的 href。優先用統編精準命中，再依序 fallback。
    link = page.locator(f"a.hover[href$='/fts/company/{tax_id}']").first
    if await link.count() == 0:
        link = page.locator("a.hover[href*='/fts/company/']").first
    if await link.count() == 0:
        link = page.locator("a.hover[href*='queryCmpyDetail']").first  # 舊版相容
    if await link.count() == 0:
        log.warning("findbiz: no result link for %s. HTML[:1000]=\n%s", tax_id, search_html[:1000])
        return None

    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            await link.click()
    except Exception as exc:
        log.error("findbiz detail click failed: %s", exc)
        return None

    # Step 3: 等詳細頁渲染出「每股金額」欄位（不再硬綁特定容器 id）
    try:
        await page.wait_for_function(
            "() => document.body && document.body.innerText.includes('每股金額')",
            timeout=15000,
        )
    except Exception:
        log.warning("findbiz: '每股金額' not rendered for %s", tax_id)

    detail_html = await page.content()
    return detail_html


async def _run_session(session_id: str) -> None:
    session  = _sessions[session_id]
    queue: asyncio.Queue = session["queue"]
    event: asyncio.Event = session["event"]
    company_id: str = session["company_id"]
    tax_id: str     = session["tax_id"]

    try:
        display = _resolve_display()
        if not display:
            await queue.put({
                "type": "error",
                "message": (
                    "找不到可用的顯示器（DISPLAY 未設定）。\n"
                    "請在終端機執行以下指令後重新整理頁面再試：\n"
                    "systemctl --user set-environment DISPLAY=:0 && "
                    "systemctl --user restart taiwan-company"
                ),
            })
            return

        log.info("findbiz: using DISPLAY=%s", display)
        Path(FINDBIZ_PROFILE_DIR).mkdir(parents=True, exist_ok=True)
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            # persistent context：cookie（含 cf_clearance）儲存在磁碟，重複使用
            ctx = await pw.chromium.launch_persistent_context(
                FINDBIZ_PROFILE_DIR,
                headless=False,
                slow_mo=200,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = await ctx.new_page()

            await page.goto(FINDBIZ_INIT, wait_until="domcontentloaded", timeout=30000)
            if _is_cloudflare_challenge(await page.content()):
                if not await _wait_for_cloudflare(page, queue, event):
                    await queue.put({
                        "type": "error",
                        "message": "Cloudflare 驗證尚未通過，請重新執行並在 Chromium 完成驗證",
                    })
                    await ctx.close()
                    return
            else:
                await queue.put({"type": "progress", "message": "使用已儲存的 session，跳過 Cloudflare…"})

            await queue.put({"type": "progress", "message": f"驗證通過，正在搜尋統編 {tax_id}…"})

            try:
                detail_html = await _search_and_load_detail(page, tax_id)
            except _CloudflareChallenge:
                # A stale cookie can pass the landing page but fail on the search
                # POST. Re-authenticate and retry within this same user action.
                await ctx.clear_cookies()
                await page.goto(FINDBIZ_INIT, wait_until="domcontentloaded", timeout=30000)
                if not await _wait_for_cloudflare(page, queue, event):
                    await queue.put({
                        "type": "error",
                        "message": "Cloudflare 驗證尚未通過；findbiz 公司資料並非不存在",
                    })
                    await ctx.close()
                    return
                await queue.put({
                    "type": "progress",
                    "message": f"Cloudflare 驗證通過，重新搜尋統編 {tax_id}…",
                })
                detail_html = await _search_and_load_detail(page, tax_id)

            if not detail_html:
                await queue.put({
                    "type": "error",
                    "message": f"findbiz 搜尋未取得統編 {tax_id} 的公司頁面，請稍後重試",
                })
                await ctx.close()
                return

            await queue.put({"type": "progress", "message": "找到公司資料，正在解析…"})
            raw = _parse_detail_html(detail_html)
            log.info("findbiz detail parsed keys: %s", list(raw.keys()))
            await ctx.close()

        par_raw      = raw.get("每股金額(元)", "")
        no_par_value = _is_no_par_value(par_raw)
        par_value    = _parse_int(par_raw)
        total_shares = _parse_int(raw.get("已發行股份總數(股)", ""))
        capital      = _parse_int(raw.get("實收資本額(元)", ""))

        if not no_par_value and not par_value and not total_shares:
            await queue.put({"type": "error", "message": "頁面上找不到每股金額或股份總數"})
            return

        # 更新 companies.json，重算持股比例
        updates: dict = {}
        if no_par_value:
            updates["no_par_value"] = True
            updates["par_value"] = 0
        elif par_value:
            updates["no_par_value"] = False
            updates["par_value"] = par_value
        if total_shares:
            updates["total_shares"] = total_shares
        if capital:
            updates["capital"] = capital

        company = data_store.get_company(company_id)
        if company and updates:
            effective_total = total_shares or company.get("total_shares", 0) or 0
            if effective_total:
                directors = list(company.get("directors", []))
                for d in directors:
                    shares = d.get("shares", 0) or 0
                    d["ratio"] = round(shares / effective_total, 6)
                updates["directors"] = directors
            data_store.update_company(company_id, updates)
            log.info(
                "findbiz: updated company %s par_value=%s no_par_value=%s total_shares=%s",
                company_id, par_value, no_par_value, total_shares,
            )

        parts = []
        if no_par_value:
            parts.append("每股金額：無票面金額")
        elif par_value:
            parts.append(f"每股金額 NT${par_value:,} 元")
        if total_shares:
            parts.append(f"已發行股份 {total_shares:,} 股")
        await queue.put({
            "type": "done",
            "updates": updates,
            "message": "，".join(parts) or "已更新",
        })

    except Exception as exc:
        log.exception("findbiz session %s error", session_id)
        await queue.put({"type": "error", "message": f"發生錯誤：{exc}"})
    finally:
        session["done"] = True


@router.post("/scrape")
async def start_scrape(req: ScrapeRequest):
    """啟動一次 findbiz 爬取 session，立即回傳 session_id。"""
    session_id = uuid.uuid4().hex[:10]
    _sessions[session_id] = {
        "queue":      asyncio.Queue(),
        "event":      asyncio.Event(),
        "company_id": req.company_id,
        "tax_id":     req.tax_id,
        "done":       False,
    }
    _spawn(_run_session(session_id))
    return {"session_id": session_id}


@router.post("/confirm/{session_id}")
async def confirm_cloudflare(session_id: str):
    """使用者通知「已通過 Cloudflare」，讓後台繼續爬取。"""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session["event"].set()
    return {"ok": True}


@router.get("/stream/{session_id}")
async def stream_session(session_id: str):
    """SSE：推送爬取進度與結果。"""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def generate() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = session["queue"]
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=60.0)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                    if msg.get("type") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield 'data: {"type":"heartbeat"}\n\n'
                    if session.get("done"):
                        break
        finally:
            _sessions.pop(session_id, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
