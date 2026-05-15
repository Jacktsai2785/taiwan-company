"""
findbiz.py — 透過 Playwright 手動通過 Cloudflare，抓取 findbiz.nat.gov.tw 的每股金額。

流程：
  POST /api/findbiz/scrape   → 啟動 Playwright browser，回傳 session_id
  GET  /api/findbiz/stream/{session_id}  → SSE 進度推送
  POST /api/findbiz/confirm/{session_id} → 使用者通知「已通過 Cloudflare」
"""
import asyncio
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


class ScrapeRequest(BaseModel):
    company_id: str
    tax_id: str


def _parse_int(s: str) -> int:
    if not s:
        return 0
    return int(re.sub(r"[^\d]", "", s) or "0")


def _parse_detail_html(html: str) -> dict:
    """
    從 findbiz detail page HTML 解析 tabCmpyContent 的 key-value。
    findbiz 是 server-side render，不需要 JS 執行。
    用標準庫 html.parser 避免依賴 BeautifulSoup。
    """
    from html.parser import HTMLParser

    class _TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_tab = False
            self.depth  = 0          # 從 tabCmpyContent 開始計 td 層級
            self.cells: list[str] = []
            self._cur  = ""
            self._in_td = False
            self.result: dict[str, str] = {}

        def handle_starttag(self, tag, attrs):
            attr_dict = dict(attrs)
            if tag == "div" and attr_dict.get("id") == "tabCmpyContent":
                self.in_tab = True
            if not self.in_tab:
                return
            if tag == "td":
                self._in_td = True
                self._cur   = ""
            if tag == "tr":
                self.cells = []

        def handle_endtag(self, tag):
            if not self.in_tab:
                return
            if tag == "td" and self._in_td:
                self.cells.append(self._cur.strip())
                self._in_td = False
            if tag == "tr" and len(self.cells) >= 2:
                key, val = self.cells[0], self.cells[1]
                if key:
                    self.result[key] = val
            if tag == "div" and self.in_tab:
                self.in_tab = False   # 離開 tab div

        def handle_data(self, data):
            if self._in_td:
                self._cur += data

        def handle_entityref(self, name):
            import html as h
            if self._in_td:
                self._cur += h.unescape(f"&{name};")

        def handle_charref(self, name):
            import html as h
            if self._in_td:
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
    1. POST 搜尋 → 等搜尋結果頁
    2. 點擊 <a class="hover"> 連結 → JS 自動 POST detailForm（不能 GET）
    3. 等 #tabCmpyContent tr 出現 → 回傳 detail 頁 HTML
    """
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
        try:
            Path("/tmp/findbiz_debug.html").write_text(search_html, encoding="utf-8")
        except Exception:
            pass
    except Exception as exc:
        log.error("findbiz search form submit failed: %s", exc)
        return None

    if "just a moment" in search_html[:300].lower():
        log.warning("findbiz: still cloudflare after search. HTML[:200]=%s", search_html[:200])
        return None

    # Step 2: 點擊第一個公司連結（讓 JS detailForm 以 POST 提交）
    link = page.locator("a.hover[href*='queryCmpyDetail']").first
    if await link.count() == 0:
        log.warning("findbiz: no result link for %s. HTML[:1000]=\n%s", tax_id, search_html[:1000])
        return None

    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            await link.click()
    except Exception as exc:
        log.error("findbiz detail click failed: %s", exc)
        return None

    # Step 3: 等 JS 填入 tabCmpyContent table
    try:
        await page.wait_for_selector("#tabCmpyContent tr", timeout=15000)
    except Exception:
        log.warning("findbiz: #tabCmpyContent tr not found for %s", tax_id)

    detail_html = await page.content()
    try:
        Path("/tmp/findbiz_detail_debug.html").write_text(detail_html, encoding="utf-8")
    except Exception:
        pass
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

            # 檢查是否有未過期的 cf_clearance
            cookies = await ctx.cookies("https://findbiz.nat.gov.tw")
            cf_cookie = next(
                (c for c in cookies if c["name"] == "cf_clearance"), None
            )
            cf_valid = cf_cookie is not None  # 有就先試；失效時底下再處理

            if not cf_valid:
                await page.goto(FINDBIZ_INIT, timeout=30000)
                await queue.put({
                    "type": "browser_ready",
                    "message": "Chromium 已開啟，請在瀏覽器中完成 Cloudflare 驗證（點擊「驗證您是真人」），完成後系統自動繼續⋯",
                })
                # 等待驗證：偵測 cf_clearance cookie 被寫入
                for _ in range(60):
                    if event.is_set():
                        break
                    cookies = await ctx.cookies("https://findbiz.nat.gov.tw")
                    if any(c["name"] == "cf_clearance" for c in cookies):
                        break
                    await asyncio.sleep(2)
                # 再次確認
                cookies = await ctx.cookies("https://findbiz.nat.gov.tw")
                if not any(c["name"] == "cf_clearance" for c in cookies):
                    await queue.put({"type": "error", "message": "Cloudflare 驗證超時，請重試"})
                    await ctx.close()
                    return
            else:
                await queue.put({"type": "progress", "message": "使用已儲存的 session，跳過 Cloudflare…"})
                await page.goto(FINDBIZ_INIT, timeout=30000)

            await queue.put({"type": "progress", "message": f"驗證通過，正在搜尋統編 {tax_id}…"})

            detail_html = await _search_and_load_detail(page, tax_id)
            if not detail_html:
                await ctx.clear_cookies()
                await queue.put({
                    "type": "error",
                    "message": f"findbiz 查無統編 {tax_id}（若 session 已過期，再試一次以重新驗證）",
                })
                await ctx.close()
                return

            await queue.put({"type": "progress", "message": "找到公司資料，正在解析…"})
            raw = _parse_detail_html(detail_html)
            try:
                Path("/tmp/findbiz_detail_parsed.txt").write_text(
                    "\n".join(f"{k}: {v}" for k, v in raw.items()), encoding="utf-8"
                )
            except Exception:
                pass
            log.info("findbiz detail parsed keys: %s", list(raw.keys()))
            await ctx.close()

        par_value    = _parse_int(raw.get("每股金額(元)", ""))
        total_shares = _parse_int(raw.get("已發行股份總數(股)", ""))
        capital      = _parse_int(raw.get("實收資本額(元)", ""))

        if not par_value and not total_shares:
            await queue.put({"type": "error", "message": "頁面上找不到每股金額或股份總數"})
            return

        # 更新 companies.json，重算持股比例
        updates: dict = {}
        if par_value:
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
            log.info("findbiz: updated company %s par_value=%s total_shares=%s", company_id, par_value, total_shares)

        parts = []
        if par_value:
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
    asyncio.create_task(_run_session(session_id))
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

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
