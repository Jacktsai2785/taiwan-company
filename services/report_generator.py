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

_CLAUDE_LOCK = asyncio.Semaphore(1)  # CLI mode: serialize to avoid concurrent session limit

_CORP_SUFFIXES = ("股份有限公司", "有限公司")


def _company_name_variants(name: str) -> tuple[str, str]:
    short = name
    for sfx in _CORP_SUFFIXES:
        if short.endswith(sfx):
            short = short[:-len(sfx)]
            break
    full = name if any(name.endswith(s) for s in _CORP_SUFFIXES) else name + "股份有限公司"
    return short, full


def _roc_to_ce(roc: str) -> str:
    """Convert 民國 YYYMMDD string to 西元 YYYY-MM-DD; return input unchanged if not that format."""
    if len(roc) != 7:
        return roc
    try:
        y, m, d = int(roc[:3]) + 1911, int(roc[3:5]), int(roc[5:7])
        return f"{y}-{m:02d}-{d:02d}"
    except Exception:
        return roc

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
    setup_date = company.get("setup_date", "")
    last_change = company.get("last_change_date", "")
    website    = (company.get("website") or "").strip()

    capital_str      = f"NT$ {capital:,}" if capital else "不詳"
    auth_capital_str = f"NT$ {auth_cap:,}" if auth_cap else "不詳"
    dir_summary = "、".join(
        f"{d['name']}（{d['title']}）" for d in directors[:5] if d.get("name")
    ) or "不詳"

    short_name, full_name = _company_name_variants(name)
    setup_ce  = _roc_to_ce(setup_date)
    change_ce = _roc_to_ce(last_change)

    # Hint: if last_change is set and differs from setup, company may have had a name or structure change
    name_change_hint = ""
    if last_change and last_change != setup_date and last_change != "1911000":
        name_change_hint = (
            f"\n注意：此公司最後核准變更日期為 {change_ce}，可能曾有公司名稱或組織形式變更。"
            f"搜尋時亦請嘗試「{short_name}有限公司」以找到使用舊名的網頁或媒體報導。"
        )

    if website:
        base = website.rstrip("/")
        step1 = f"步驟 1：直接用 WebFetch 讀取已知官網 {base}（首頁），取得具體服務項目、目標客戶、技術說明。"
        step2 = f"步驟 2：若首頁資訊不足，繼續用 WebFetch 讀取 {base}/about 或 {base}/products 等子頁補充細節。"
    else:
        step1 = (
            f"步驟 1：用 WebSearch 搜尋「{full_name}」的官方網站與公司介紹。\n"
            f"        若無明確結果，改搜「{short_name} 公司 官網」或「{short_name} Taiwan」。{name_change_hint}"
        )
        step2 = (
            f"步驟 2：若步驟 1 找到官網 URL，用 WebFetch 讀取該網站首頁（或 /about、/products、/service 等子頁），\n"
            f"        取得具體服務項目、目標客戶、技術說明。"
        )

    return f"""你是一位在台灣有豐富經驗的資深私募股權（PE）投資人，正對以下公司進行初步盡職調查（Due Diligence）。

【基本資料】
公司名稱：{full_name}（簡稱：{short_name}）
統一編號：{tax_id or "不詳"}
核准設立日期：{setup_ce}
所屬產業：{industry}
代表人：{rep}
實收資本額：{capital_str}
資本總額：{auth_capital_str}
所在地：{address}
上市狀態：{listing}
主要董監事：{dir_summary}

【任務】
請依序執行以下所有搜尋步驟，每步都要執行，找不到結果時繼續往下，不要中途停止：

{step1}

{step2}

步驟 3：用 WebSearch 搜尋「{short_name} 報導 OR 新聞 OR 採訪 OR 入選 OR 獲獎 OR 媒體 OR 創業」，
        找媒體報導、政府補助入選名單、育成中心、加速器等第三方資訊。

步驟 4：根據前面搜尋所掌握的業務性質，用 WebSearch 搜尋「台灣 {industry if industry != "不詳" else "[依業務性質自行推斷產業關鍵字]"} 競爭廠商 股份有限公司」，找出至少 3 家台灣同業競爭者。

完成搜尋後，用繁體中文撰寫以下格式的投資備忘錄（純 Markdown，不加開頭標題行）：

## 業務概況
（根據官網、104公司頁、媒體報導或其他可信來源，具體說明：主要產品或服務項目、核心技術、主要客戶群或市場。
若所有搜尋管道均無資料，各項目直接標注「——」並在段末一行說明資訊侷限；
禁止以段落解釋自己的搜尋過程，禁止使用「可能」、「推測」等模糊語氣填充內容。）

## 競業分析

| 公司名稱 | 核心業務 | 主要差異化特點 | 上市狀態 |
|------|------|------|------|
| {full_name}（本案）| （填入） | （填入） | {listing} |
（補充至少 3 家台灣競業列）

（表格後以條列說明：本案在市場中的相對優勢 2-3 點、相對劣勢或挑戰 2-3 點。若有已知專利或技術壁壘請一併提及。）

## 主要風險
（列點，3-5 項，需具體指向該公司業務或產業，禁止寫「市場競爭激烈」、「法規風險」等空泛語句）

---
目標字數：600-900 字（不含表格）
**嚴格禁止在備忘錄末尾輸出任何 Sources、References、來源清單或 URL 列表。**

最後單獨一行，格式固定如下（不超過10個繁體中文字，描述核心產品或服務，禁止出現公司名稱）：
[blurb: 核心產品或服務描述]"""


def _build_deep_prompt(company: dict) -> str:
    name = company.get("name", "")
    existing = (company.get("summary") or "").strip()
    website  = (company.get("website") or "").strip()

    short_name, full_name = _company_name_variants(name)

    base = f"以下是「{full_name}」的初步投資備忘錄：\n\n{existing}\n\n---\n\n" if existing else ""

    website_step = (
        f"步驟 0：先用 WebFetch 讀取官網 {website}（首頁與 /about、/products 等子頁），"
        f"確認初步備忘錄的業務描述是否正確，有誤則依官網內容修正。\n\n"
    ) if website else ""

    return f"""你是一位在台灣有豐富經驗的資深私募股權（PE）投資人。

{base}請執行深度補充搜尋，並據此修訂備忘錄：

{website_step}步驟 1：用 WebSearch 搜尋「{short_name} 報導 OR 新聞 OR 採訪 OR 入選 OR 獲獎 OR 媒體 OR 創業」，
        找媒體報導、政府補助入選名單、育成中心、加速器等第三方資訊。

步驟 2：若找到相關資訊，用 WebFetch 讀取最有參考價值的 1-2 篇文章全文。

完成搜尋後，輸出修訂版完整備忘錄（格式：## 業務概況、## 競業分析、## 主要風險）。
若新資料提供了原版沒有的具體資訊，更新對應段落；若無新資訊，維持原內容。
**嚴格禁止在末尾輸出任何 Sources、References、來源清單或 URL 列表。**

最後單獨一行，格式固定如下（不超過10個繁體中文字，描述核心產品或服務，禁止出現公司名稱）：
[blurb: 核心產品或服務描述]"""


async def deep_enrich_summary(company: dict, api_key: str = "", provider: str = "anthropic") -> dict:
    """Search news/media and refine the existing summary. Returns {summary, blurb}."""
    name = company.get("name", "")
    prompt = _build_deep_prompt(company)
    async with _CLAUDE_LOCK:
        try:
            raw = await asyncio.to_thread(
                claude_client.ask, prompt, 300, _WEB_TOOLS, api_key, provider, 15
            )
            summary, blurb = _split_blurb(raw)
            if not blurb and len(summary.strip()) > 100:
                blurb = await _generate_blurb_fallback(summary, name, api_key, provider)
            return {"summary": summary, "blurb": blurb}
        except Exception as e:
            raise RuntimeError(f"深度生成失敗：{e}") from e


async def generate_summary(company: dict, api_key: str = "", provider: str = "anthropic") -> dict:
    """
    Returns a due-diligence memo in Traditional Chinese Markdown.
    Retries once on failure with a 5-second gap.
    """
    name = company.get("name", "")
    prompt = _build_prompt(company)

    last_error: Exception | None = None
    async with _CLAUDE_LOCK:
        for attempt in range(2):
            try:
                log.info("Generating DD memo for %s (attempt %d)", name, attempt + 1)
                raw = await asyncio.to_thread(
                    claude_client.ask, prompt, 240, _WEB_TOOLS, api_key, provider, 12
                )
                summary, blurb = _split_blurb(raw)

                # Fallback: if blurb missing but summary has real content, generate quickly
                if not blurb and len(summary.strip()) > 100:
                    log.info("Blurb missing for %s, running fallback generation", name)
                    blurb = await _generate_blurb_fallback(summary, name, api_key, provider)

                log.info("DD memo done for %s (%d chars, blurb=%r)", name, len(summary), blurb)
                return {"summary": summary, "blurb": blurb}
            except Exception as e:
                last_error = e
                log.warning("DD memo attempt %d failed for %s: %s", attempt + 1, name, e)
                if attempt == 0:
                    await asyncio.sleep(5)

    # Both attempts failed — surface the underlying error so the SSE caller can
    # show it to the user instead of pretending generation succeeded.
    raise RuntimeError(f"公司簡介生成失敗：{last_error}") from last_error


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

    cleaned = raw.strip()

    # Strip Claude status preamble before the first ## heading (e.g. "資料已蒐集完畢，開始撰寫投資備忘錄。\n\n---\n\n")
    cleaned = re.sub(r'^(?!#+\s).*\n+[-—]{3,}\n+', '', cleaned, count=1)

    # Remove any trailing Sources/References block Claude sometimes appends
    cleaned = re.sub(
        r'\n+(?:Sources?|References?|來源|參考資料)\s*:?.*$',
        '',
        cleaned,
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
