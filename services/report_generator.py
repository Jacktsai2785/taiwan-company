"""
Generate company due-diligence memos via local Claude Code CLI.
Claude is allowed to use WebSearch + WebFetch to retrieve live data.

A global asyncio.Lock serializes calls so two enrichment tasks don't
spawn competing Claude CLI processes simultaneously.
"""
import asyncio
import logging

from . import claude_client

log = logging.getLogger("report_generator")

_CLAUDE_LOCK = asyncio.Semaphore(3)

_WEB_TOOLS = ["WebSearch", "WebFetch"]


def _build_prompt(company: dict) -> str:
    name       = company.get("name", "")
    industry   = company.get("industry", "") or "不詳"
    address    = company.get("address", "") or "不詳"
    capital    = company.get("capital", 0)
    auth_cap   = company.get("authorized_capital", 0)
    rep        = company.get("representative", "") or "不詳"
    listing    = company.get("listing_status", "非公發")
    tax_id     = company.get("tax_id", "")
    directors  = company.get("directors", [])

    capital_str      = f"NT$ {capital:,}" if capital else "不詳"
    auth_capital_str = f"NT$ {auth_cap:,}" if auth_cap else "不詳"
    dir_summary = "、".join(
        f"{d['name']}（{d['title']}）" for d in directors[:5] if d.get("name")
    ) or "不詳"

    return f"""你是一位在台灣有豐富經驗的資深私募股權（PE）投資人，正對以下公司進行初步盡職調查（Due Diligence）。

【基本資料】
公司名稱：{name}
統一編號：{tax_id or "不詳"}
所屬產業：{industry}
代表人：{rep}
實收資本額：{capital_str}
資本總額：{auth_capital_str}
所在地：{address}
上市狀態：{listing}
主要董監事：{dir_summary}

【任務】
請依序執行：
1. 用 WebSearch 搜尋「{name}」的官方網站、產品/服務介紹、近期新聞（2022年後）。
2. 用 WebFetch 讀取官網首頁或產品頁，取得具體業務資訊。
3. 用 WebSearch 搜尋「台灣 {industry} 競爭廠商 股份有限公司」，找出至少 3 家台灣同業競爭者。

完成搜尋後，用繁體中文撰寫以下格式的投資備忘錄（純 Markdown，不加開頭標題行）：

## 業務概況
（根據官網或可信外部資料，具體說明：主要產品或服務項目、核心技術、主要客戶群或市場。
若官網資訊不足，請明確標注「官網未公開」，禁止使用「可能」、「推測」等模糊語氣填充內容。）

## 競業分析

| 公司名稱 | 核心業務 | 主要差異化特點 | 上市狀態 |
|------|------|------|------|
| {name}（本案）| （填入） | （填入） | {listing} |
（補充至少 3 家台灣競業列）

（表格後以條列說明：本案在市場中的相對優勢 2-3 點、相對劣勢或挑戰 2-3 點。若有已知專利或技術壁壘請一併提及。）

## 主要風險
（列點，3-5 項，需具體指向該公司業務或產業，禁止寫「市場競爭激烈」、「法規風險」等空泛語句）

---
目標字數：600-900 字（不含表格）
**嚴格禁止在備忘錄末尾輸出任何 Sources、References、來源清單或 URL 列表。**

最後單獨一行，格式固定如下（不超過10個繁體中文字，描述核心產品或服務，禁止出現公司名稱）：
[blurb: 核心產品或服務描述]"""


async def generate_summary(company: dict, api_key: str = "", provider: str = "anthropic") -> dict:
    """
    Returns a due-diligence memo in Traditional Chinese Markdown.
    Retries once on failure with a 5-second gap.
    """
    name = company.get("name", "")
    prompt = _build_prompt(company)

    async with _CLAUDE_LOCK:
        for attempt in range(2):
            try:
                log.info("Generating DD memo for %s (attempt %d)", name, attempt + 1)
                raw = await asyncio.to_thread(
                    claude_client.ask, prompt, 300, _WEB_TOOLS, api_key, provider
                )
                summary, blurb = _split_blurb(raw)

                # Fallback: if blurb missing but summary has real content, generate quickly
                if not blurb and len(summary.strip()) > 100:
                    log.info("Blurb missing for %s, running fallback generation", name)
                    blurb = await _generate_blurb_fallback(summary, name, api_key, provider)

                log.info("DD memo done for %s (%d chars, blurb=%r)", name, len(summary), blurb)
                return {"summary": summary, "blurb": blurb}
            except Exception as e:
                log.warning("DD memo attempt %d failed for %s: %s", attempt + 1, name, e)
                if attempt == 0:
                    await asyncio.sleep(5)

    return {"summary": "（公司簡介尚待補充，請稍後手動重試）", "blurb": ""}


async def _generate_blurb_fallback(summary: str, name: str, api_key: str = "", provider: str = "anthropic") -> str:
    """Quick fallback: generate ≤10-char blurb from existing summary text."""
    import re
    prompt = (
        f"請用不超過10個繁體中文字描述以下公司的核心產品或服務。"
        f"禁止出現公司名稱「{name}」。只輸出描述本身，不加標點、引號或說明文字。\n\n"
        f"{summary[:500]}"
    )
    try:
        raw = await asyncio.to_thread(claude_client.ask, prompt, 30, None, api_key, provider)
        blurb = raw.strip().split("\n")[0].strip()
        m = re.match(r'^\[blurb:\s*(.+?)\]\s*$', blurb)
        if m:
            blurb = m.group(1).strip()
        return blurb[:15]
    except Exception as e:
        log.warning("Blurb fallback failed for %s: %s", name, e)
        return ""


def _split_blurb(raw: str) -> tuple[str, str]:
    """Extract [blurb: ...] from output; strip trailing Sources section; return (summary, blurb)."""
    import re

    # Remove any trailing Sources/References block Claude sometimes appends
    cleaned = re.sub(
        r'\n+(?:Sources?|References?|來源|參考資料)\s*:?.*$',
        '',
        raw.strip(),
        flags=re.DOTALL | re.IGNORECASE,
    ).rstrip()

    m = re.search(r'\[blurb:\s*(.+?)\]\s*$', cleaned, re.MULTILINE)
    if m:
        blurb = m.group(1).strip()
        summary = cleaned[:m.start()].rstrip()
    else:
        blurb = ""
        summary = cleaned
    return summary, blurb
