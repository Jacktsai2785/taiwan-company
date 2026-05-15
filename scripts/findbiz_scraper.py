#!/usr/bin/env python3
"""
findbiz_scraper.py — 手動通過 Cloudflare，從 findbiz.nat.gov.tw 抓取每股金額

用法：
    # 查詢指定統編（空白分隔）
    python scripts/findbiz_scraper.py 84149738 60737697

    # 自動掃描 companies.json 中缺少每股金額的股份有限公司並更新
    python scripts/findbiz_scraper.py --update-missing

流程：
    1. 開啟 Chromium 視窗，導向 findbiz
    2. 你手動通過 Cloudflare challenge（點 I'm not a robot 或等待）
    3. 通過後在終端機按 Enter，腳本接手爬資料
    4. 輸出 JSON，或直接寫回 companies.json

抓到的欄位：
    每股金額(元)、已發行股份總數(股)、資本總額(元)、實收資本額(元)
    代表人姓名、公司所在地、核准設立日期、最後核准變更日期
    董監事名單（序號、職稱、姓名、所代表法人、出資額）
"""

import asyncio
import json
import re
import sys
from pathlib import Path

FINDBIZ_BASE = "https://findbiz.nat.gov.tw"
INIT_URL     = f"{FINDBIZ_BASE}/fts/query/QueryBar/queryInit.do"
LIST_URL     = f"{FINDBIZ_BASE}/fts/query/QueryList/queryList.do"
DATA_FILE    = Path(__file__).parent.parent / "data" / "companies.json"


# ── 解析公司詳細頁 ──────────────────────────────────────────────────────────────

def _parse_int(s: str) -> int:
    if not s:
        return 0
    return int(re.sub(r"[^\d]", "", s) or "0")


async def _parse_detail_page(page) -> dict:
    """從已開啟的 detail page 抽取基本資料 + 董監事。"""
    result = {}

    # 基本資料：tabCmpyContent 裡的 tr > td[0]=欄位名, td[1]=值
    rows = await page.query_selector_all("#tabCmpyContent tbody tr")
    for row in rows:
        tds = await row.query_selector_all("td")
        if len(tds) < 2:
            continue
        key   = (await tds[0].inner_text()).strip()
        value = (await tds[1].inner_text()).strip()
        if key:
            result[key] = value

    # 董監事：tabShareHolderContent
    directors = []
    dir_rows = await page.query_selector_all("#tabShareHolderContent tbody tr")
    for row in dir_rows:
        tds = await row.query_selector_all("td")
        if len(tds) < 5:
            continue
        seq   = (await tds[0].inner_text()).strip()
        title = (await tds[1].inner_text()).strip()
        name  = (await tds[2].inner_text()).strip()
        juris = (await tds[3].inner_text()).strip()
        amt   = (await tds[4].inner_text()).strip()
        if seq:
            directors.append({
                "seq": seq, "title": title, "name": name,
                "juristic": juris, "shares_raw": amt,
            })
    if directors:
        result["_directors"] = directors

    return result


def _extract_fields(raw: dict) -> dict:
    """把 findbiz 的中文 key 轉成我們 companies.json 用的 key。"""
    par   = _parse_int(raw.get("每股金額(元)", ""))
    total = _parse_int(raw.get("已發行股份總數(股)", ""))
    cap   = _parse_int(raw.get("實收資本額(元)", ""))

    directors = []
    for d in raw.get("_directors", []):
        shares = _parse_int(d["shares_raw"])
        directors.append({
            "title":   d["title"],
            "name":    d["name"],
            "juristic": d["juristic"],
            "shares":  shares,
            "ratio":   round(shares / total, 6) if total else None,
        })

    return {
        "par_value":    par   or None,
        "total_shares": total or None,
        "capital":      cap   or None,
        "representative": raw.get("代表人姓名", ""),
        "address":      raw.get("公司所在地", ""),
        "directors_findbiz": directors,   # 獨立 key，不覆蓋原 directors
        # 原始欄位備查
        "_raw_par":   raw.get("每股金額(元)", ""),
        "_raw_total": raw.get("已發行股份總數(股)", ""),
    }


# ── findbiz 搜尋流程 ──────────────────────────────────────────────────────────

async def _search_and_get_detail_url(page, tax_id: str) -> str | None:
    """POST 搜尋統編，回傳 detail page URL（含 objectId）。"""
    await page.goto(LIST_URL, wait_until="networkidle")

    # 填表單
    await page.fill('input[name="qryCond"]', tax_id)
    # 確保 cmpyType checkbox 被勾選
    cb = page.locator('input[name="cmpyType"]')
    if not await cb.is_checked():
        await cb.check()

    await page.click('button[type="submit"], input[type="submit"]')
    await page.wait_for_load_state("networkidle")

    # 找 detail 連結
    link = await page.query_selector('a[href*="queryCmpyDetail"]')
    if not link:
        return None
    href = await link.get_attribute("href")
    return FINDBIZ_BASE + href if href.startswith("/") else href


async def scrape_one(page, tax_id: str) -> dict | None:
    """搜尋單一統編，回傳解析後的欄位 dict；找不到回傳 None。"""
    print(f"  查詢 {tax_id} …", end=" ", flush=True)

    detail_url = await _search_and_get_detail_url(page, tax_id)
    if not detail_url:
        print("找不到")
        return None

    await page.goto(detail_url, wait_until="networkidle")
    raw = await _parse_detail_page(page)
    if not raw:
        print("頁面解析失敗")
        return None

    fields = _extract_fields(raw)
    par   = fields.get("par_value")
    total = fields.get("total_shares")
    print(f"每股={par}  已發行={total:,}" if total else f"每股={par}  已發行=無")
    return {"tax_id": tax_id, **fields}


# ── 主流程 ────────────────────────────────────────────────────────────────────

async def main():
    from playwright.async_api import async_playwright

    args = sys.argv[1:]
    update_missing = "--update-missing" in args
    tax_ids = [a for a in args if not a.startswith("--")]

    if update_missing:
        # 從 companies.json 找缺少 par_value 的股份有限公司
        if not DATA_FILE.exists():
            print(f"找不到 {DATA_FILE}")
            sys.exit(1)
        companies = json.loads(DATA_FILE.read_text())
        missing = [
            c for c in companies
            if c.get("name", "").endswith("股份有限公司")
            and not c.get("par_value")
            and c.get("tax_id")
        ]
        if not missing:
            print("沒有需要更新的公司")
            return
        print(f"找到 {len(missing)} 家缺少每股金額的股份有限公司：")
        for c in missing:
            print(f"  {c['name']} ({c['tax_id']})")
        tax_ids = [c["tax_id"] for c in missing]
    elif not tax_ids:
        print(__doc__)
        sys.exit(1)

    print()
    print("開啟 Chromium，請手動通過 Cloudflare 驗證…")
    print("通過後（看到 findbiz 正常頁面），回到這個終端機按 Enter 繼續。")
    print()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=300)
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        # 導向 findbiz，讓使用者通過 Cloudflare
        await page.goto(INIT_URL)

        # 等待使用者確認
        print(">> ", end="", flush=True)
        await asyncio.get_event_loop().run_in_executor(None, input)

        # 確認已通過（檢查是否還在 Cloudflare challenge 頁）
        title = await page.title()
        if "Just a moment" in title or "Cloudflare" in title:
            print("偵測到仍在 Cloudflare 驗證頁，請重新執行後再試。")
            await browser.close()
            return

        print(f"\n開始爬取 {len(tax_ids)} 筆…\n")
        scraped: list[dict] = []
        for tid in tax_ids:
            result = await scrape_one(page, tid)
            if result:
                scraped.append(result)
            await asyncio.sleep(1)  # 每筆之間停頓，避免被擋

        await browser.close()

    # ── 輸出 ──────────────────────────────────────────────────────────────────
    if not scraped:
        print("\n沒有成功抓到任何資料。")
        return

    print(f"\n成功抓到 {len(scraped)} 筆。")

    if update_missing and DATA_FILE.exists():
        # 寫回 companies.json
        companies = json.loads(DATA_FILE.read_text())
        idx = {c["tax_id"]: i for i, c in enumerate(companies) if c.get("tax_id")}
        updated = 0
        for item in scraped:
            tid = item["tax_id"]
            if tid not in idx:
                continue
            co = companies[idx[tid]]
            if item.get("par_value"):
                co["par_value"] = item["par_value"]
            if item.get("total_shares"):
                co["total_shares"] = item["total_shares"]
                # 重算持股比例
                for d in co.get("directors", []):
                    shares = d.get("shares", 0) or 0
                    d["ratio"] = round(shares / item["total_shares"], 6)
            updated += 1

        DATA_FILE.write_text(json.dumps(companies, ensure_ascii=False, indent=2))
        print(f"已更新 {updated} 家公司的 companies.json。")
    else:
        # 只印 JSON
        print()
        print(json.dumps(scraped, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
