"""
一次性批次腳本：掃描 companies.json，對所有缺少 par_value 的公司
透過 Playwright 連到 findbiz.nat.gov.tw 抓取每股金額，直接寫回 JSON。

用法：
  cd ~/taiwan-company
  DISPLAY=:0 .venv/bin/python scripts/batch_fetch_par_value.py
"""
import asyncio
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "companies.json"
FINDBIZ_PROFILE_DIR = str(ROOT / "data" / "findbiz_profile")
FINDBIZ_INIT = "https://findbiz.nat.gov.tw/"
FINDBIZ_LIST = "https://findbiz.nat.gov.tw/fts/query/QueryList/queryList.do"


def _parse_int(s: str) -> int:
    if not s:
        return 0
    return int(re.sub(r"[^\d]", "", s) or "0")


def _parse_detail_html(html: str) -> dict:
    class _TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.cells: list[str] = []
            self._cur = ""
            self._in_cell = False
            self.result: dict[str, str] = {}

        def handle_starttag(self, tag, attrs):
            if tag in ("td", "th"):
                self._in_cell = True
                self._cur = ""
            elif tag == "tr":
                self.cells = []

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self._in_cell:
                self.cells.append(self._cur.strip())
                self._in_cell = False
            elif tag == "tr" and len(self.cells) >= 2:
                key, val = self.cells[0], self.cells[1]
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


async def search_and_load_detail(page, tax_id: str) -> str | None:
    params = {
        "errorMsg": "", "validatorOpen": "N", "rlPermit": "0",
        "userResp": "", "curPage": "0", "fhl": "zh_TW",
        "qryCond": tax_id, "infoType": "D",
        "qryType": "cmpyType", "cmpyType": "true",
        "brCmpyType": "", "busmType": "", "factType": "",
        "lmtdType": "", "isAlive": "all",
        "busiItemMain": "", "busiItemSub": "",
    }
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
            await page.evaluate(
                """([url, params]) => {
                    const form = document.createElement('form');
                    form.method = 'POST'; form.action = url;
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
        print(f"  [搜尋失敗] {exc}")
        return None

    if "just a moment" in search_html[:300].lower():
        print("  [Cloudflare 仍在攔截，session 可能過期]")
        return None

    link = page.locator(f"a.hover[href$='/fts/company/{tax_id}']").first
    if await link.count() == 0:
        link = page.locator("a.hover[href*='/fts/company/']").first
    if await link.count() == 0:
        link = page.locator("a.hover[href*='queryCmpyDetail']").first
    if await link.count() == 0:
        print(f"  [查無結果連結]")
        return None

    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            await link.click()
    except Exception as exc:
        print(f"  [點擊詳細頁失敗] {exc}")
        return None

    try:
        await page.wait_for_function(
            "() => document.body && document.body.innerText.includes('每股金額')",
            timeout=15000,
        )
    except Exception:
        print("  [等待「每股金額」渲染超時]")

    return await page.content()


async def main():
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    companies = data["companies"]

    missing = [
        c for c in companies
        if not c.get("par_value")
        and not c.get("no_par_value")
        and c.get("tax_id")
        and c.get("is_corp", True)
    ]

    if not missing:
        print("所有公司已有每股金額，無需抓取。")
        return

    print(f"共 {len(missing)} 間公司缺少每股金額：")
    for c in missing:
        print(f"  - {c['name']} ({c['tax_id']})")
    print()

    Path(FINDBIZ_PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            FINDBIZ_PROFILE_DIR,
            headless=False,
            slow_mo=200,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await ctx.new_page()

        cookies = await ctx.cookies("https://findbiz.nat.gov.tw")
        cf_valid = any(c["name"] == "cf_clearance" for c in cookies)

        if not cf_valid:
            await page.goto(FINDBIZ_INIT, timeout=30000)
            print("瀏覽器已開啟。請在瀏覽器中完成 Cloudflare 驗證（點「驗證您是真人」）。")
            print("驗證通過後腳本自動繼續，最多等 120 秒…")
            for _ in range(60):
                await asyncio.sleep(2)
                cookies = await ctx.cookies("https://findbiz.nat.gov.tw")
                if any(c["name"] == "cf_clearance" for c in cookies):
                    print("Cloudflare 驗證通過！")
                    break
            else:
                print("驗證超時，請重新執行腳本。")
                await ctx.close()
                sys.exit(1)
        else:
            print("使用已儲存的 session，跳過 Cloudflare 驗證。")
            await page.goto(FINDBIZ_INIT, timeout=30000)

        print()
        total = len(missing)
        ok, fail = 0, 0
        failed_names = []

        for i, company in enumerate(missing):
            name = company["name"]
            tax_id = company["tax_id"]
            print(f"[{i+1}/{total}] {name}（{tax_id}）…", end=" ", flush=True)

            detail_html = await search_and_load_detail(page, tax_id)
            if not detail_html:
                fail += 1
                failed_names.append(name)
                print("❌ 查無資料")
                await asyncio.sleep(1)
                continue

            raw = _parse_detail_html(detail_html)
            par_value    = _parse_int(raw.get("每股金額(元)", ""))
            total_shares = _parse_int(raw.get("已發行股份總數(股)", ""))
            capital      = _parse_int(raw.get("實收資本額(元)", ""))

            if not par_value and not total_shares:
                fail += 1
                failed_names.append(name)
                print("❌ 頁面找不到每股金額")
                await asyncio.sleep(1)
                continue

            # 寫回 companies list（直接修改 dict reference）
            if par_value:
                company["par_value"] = par_value
            if total_shares:
                company["total_shares"] = total_shares
            if capital and not company.get("capital"):
                company["capital"] = capital

            # 重算董監持股比例
            effective_total = total_shares or company.get("total_shares", 0) or 0
            if effective_total:
                for d in company.get("directors", []):
                    shares = d.get("shares", 0) or 0
                    d["ratio"] = round(shares / effective_total, 6)

            parts = []
            if par_value:
                parts.append(f"每股 NT${par_value:,}")
            if total_shares:
                parts.append(f"{total_shares:,} 股")
            print(f"✅ {', '.join(parts)}")
            ok += 1

            # 每間完成後立即存檔，避免中途中斷遺失資料
            DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            await asyncio.sleep(1)

        await ctx.close()

    print()
    print(f"批次完成：✅ {ok} 成功，❌ {fail} 失敗（共 {total} 間）")
    if failed_names:
        print("失敗清單：")
        for n in failed_names:
            print(f"  - {n}")


if __name__ == "__main__":
    asyncio.run(main())
