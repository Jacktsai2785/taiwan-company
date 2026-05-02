"""
手動對 companies.json 中所有缺少資料的公司執行 Step 5 (enrichment)。
用法：python run_enrich.py
"""
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from services import data_store, gcis_client, report_generator


async def enrich_one(company: dict) -> None:
    name = company["name"]
    cid  = company["id"]
    print(f"\n{'='*50}")
    print(f"處理：{name}")

    # ── Step 5a: GCIS ────────────────────────────────
    print("  查詢 GCIS…")
    try:
        enrichment = await gcis_client.fetch_company_data(name)
        data_store.update_company(cid, enrichment)
        print(f"  ✓ GCIS 完成：代表人={enrichment.get('representative') or '—'}, "
              f"資本額={enrichment.get('capital') or 0:,}")
    except Exception as e:
        print(f"  ✗ GCIS 失敗：{e}")

    # ── Step 5b: Summary ─────────────────────────────
    print("  生成公司簡介…")
    company = data_store.get_company(cid)   # reload after GCIS update
    try:
        summary = await report_generator.generate_summary(company)
        data_store.update_company(cid, {"summary": summary})
        preview = summary[:80].replace("\n", " ")
        print(f"  ✓ 簡介完成：{preview}…")
    except Exception as e:
        print(f"  ✗ 簡介失敗：{e}")


async def main():
    companies = data_store.get_all_companies()
    if not companies:
        print("companies.json 中沒有公司資料")
        return

    # Only enrich companies missing representative or summary
    to_enrich = [
        c for c in companies
        if not c.get("representative") or not c.get("summary")
        or "尚待補充" in (c.get("summary") or "")
    ]

    if not to_enrich:
        print("所有公司資料已完整，無需補充")
        return

    print(f"準備補充 {len(to_enrich)} 間公司的資料…")
    for c in to_enrich:
        await enrich_one(c)

    print("\n\n全部完成！請重新整理瀏覽器查看結果。")


if __name__ == "__main__":
    asyncio.run(main())
