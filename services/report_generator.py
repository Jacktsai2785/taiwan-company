"""
Generate company due-diligence memos via local Claude Code CLI.
Claude is allowed to use WebSearch + WebFetch to retrieve live data.

A global asyncio.Lock serializes calls so two enrichment tasks don't
spawn competing Claude CLI processes simultaneously.
"""
import asyncio
import logging
import re

import httpx

from . import claude_client
from .gcis_client import _ensure_listing_cache, _resolve_listing_status

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


def _build_prompt(company: dict, competitor_context: dict | None = None) -> str:
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

    # Build competitor context hints
    known_hint = ""
    known_table_rows = ""
    direct = (competitor_context or {}).get("direct") or []
    extended = (competitor_context or {}).get("extended") or []

    if direct:
        direct_list = "\n".join(
            f"  - {c['name']}（{c['blurb'] or '同業'}，上市狀態：{c.get('listing_status') or '不詳'}）"
            for c in direct
        )
        known_hint = (
            f"\n\n⚠ 特別注意：系統記錄顯示以下公司與本案存在競業關係，"
            f"請在步驟 4 搜尋時一併查閱，並**確保它們出現在競業分析表格中**：\n{direct_list}"
        )
        known_table_rows = "\n".join(
            f"| {c['name']} | （請根據搜尋結果填入） | （填入差異化特點） | {c.get('listing_status') or '非公發'} | （AI 判斷填入） |"
            for c in direct
        ) + "\n"

    if extended:
        ext_list = "\n".join(
            f"  - {c['name']}（{c['blurb'] or '同業'}，由「{c['via']}」延伸）"
            for c in extended
        )
        known_hint += (
            f"\n\n📋 延伸參考：以下公司與本案的已知競業處於同一競業圈，供 AI 判斷是否需要納入分析（不強制）：\n{ext_list}"
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

步驟 4：根據前面搜尋所掌握的業務性質，搜尋台灣競爭者——**請涵蓋以下四種競業類型**。其中「正面競業」**只填搜尋結果中能佐證的公司**（官網、媒體報導、公司登記均可）；找不到足夠家數就只填 1-2 家，**嚴格禁止捏造或推測公司名稱**；其餘三類各至少 1 家：
  - 正面競業：同產品／服務，直接搶相同客戶或標案
  - 替代路徑：客戶解決相同問題的不同技術或商業模式
  - 側翼潛入：現在不在此市場，但有能力、有誘因跨入的鄰近業者（如大型集團、跨國廠商）
  - 垂直整合：重要客戶或上游供應商有可能自行發展相同能力者
  搜尋字串建議：「台灣 {industry if industry != "不詳" else "[依業務性質自行推斷產業關鍵字]"} 競爭廠商」、「[核心技術關鍵字] 替代方案 台灣」{known_hint}

【競業表填寫規則（請嚴格遵循；但這些規則本身**禁止輸出到備忘錄裡**，你只能輸出填好的表格與分析）】
- 競業類型限填：正面競業／替代路徑／側翼潛入／垂直整合。
- 「公司名稱」欄一律填正式登記名稱（如 ○○股份有限公司／○○有限公司）。若只查到品牌或產品名（例如「超木 GREENuWood」），請查出其背後的法人公司全名填入，可在括號內附註品牌，例如「○○股份有限公司（超木 GREENuWood）」；禁止只填品牌名或英文商標，否則後續無法連結公司登記資料。
- 一列只填一家公司：每家競業各自一列，不可在同一格用「、」「／」「與」等把多家塞在一起，也不要加「上游」「下游」這類描述詞或「等」字；同一競業類型有多家就拆成多列分別填寫。

完成搜尋後，用繁體中文撰寫以下格式的投資備忘錄（純 Markdown，不加開頭標題行）：

## 業務概況
（根據官網、104公司頁、媒體報導或其他可信來源，具體說明：主要產品或服務項目、核心技術、主要客戶群或市場。
若所有搜尋管道均無資料，各項目直接標注「——」並在段末一行說明資訊侷限；
禁止以段落解釋自己的搜尋過程，禁止使用「可能」、「推測」等模糊語氣填充內容。）

## 競業分析

| 公司名稱 | 核心業務 | 主要差異化特點 | 上市狀態 | 競業類型 |
|------|------|------|------|------|
| {full_name}（本案）| （填入） | （填入） | {listing} | — |
{known_table_rows}（正面競業僅填有搜尋佐證者，1-3 家皆可；其餘三類各至少 1 家；**禁止捏造或推測公司名稱**）

（表格後以條列說明：本案在市場中的相對優勢 2-3 點、相對劣勢或挑戰 2-3 點。若有已知專利或技術壁壘請一併提及。）

## 主要風險
（列點，3-5 項，需具體指向該公司業務或產業，禁止寫「市場競爭激烈」、「法規風險」等空泛語句）

---
目標字數：600-900 字（不含表格）
**嚴格禁止在備忘錄末尾輸出任何 Sources、References、來源清單或 URL 列表。**
**嚴格禁止把上面任何「填寫規則」「欄位說明」「競業類型定義」等指示文字複製到備忘錄內容裡——只輸出填好的表格與分析。**

最後單獨一行，格式固定如下（不超過10個繁體中文字，描述核心產品或服務，禁止出現公司名稱）：
[blurb: 核心產品或服務描述]"""


def _build_deep_prompt(company: dict, competitor_context: dict | None = None) -> str:
    name = company.get("name", "")
    existing = (company.get("summary") or "").strip()
    website  = (company.get("website") or "").strip()

    short_name, full_name = _company_name_variants(name)

    base = f"以下是「{full_name}」的初步投資備忘錄：\n\n{existing}\n\n---\n\n" if existing else ""

    website_step = (
        f"步驟 0：先用 WebFetch 讀取官網 {website}（首頁與 /about、/products 等子頁），"
        f"確認初步備忘錄的業務描述是否正確，有誤則依官網內容修正。\n\n"
    ) if website else ""

    known_hint = ""
    direct = (competitor_context or {}).get("direct") or []
    extended = (competitor_context or {}).get("extended") or []

    if direct:
        direct_list = "\n".join(
            f"  - {c['name']}（{c['blurb'] or '同業'}，上市狀態：{c.get('listing_status') or '不詳'}）"
            for c in direct
        )
        known_hint = (
            f"\n\n⚠ 特別注意：系統記錄顯示以下公司與本案存在競業關係，"
            f"若競業表格中尚未包含它們，請補入並填寫差異化分析：\n{direct_list}"
        )
    if extended:
        ext_list = "\n".join(
            f"  - {c['name']}（{c['blurb'] or '同業'}，由「{c['via']}」延伸）"
            for c in extended
        )
        known_hint += (
            f"\n\n📋 延伸參考：以下公司與本案的已知競業處於同一競業圈，供 AI 判斷是否需要納入分析（不強制）：\n{ext_list}"
        )

    return f"""你是一位在台灣有豐富經驗的資深私募股權（PE）投資人。

{base}請執行深度補充搜尋，並據此修訂備忘錄：

{website_step}步驟 1：用 WebSearch 搜尋「{short_name} 報導 OR 新聞 OR 採訪 OR 入選 OR 獲獎 OR 媒體 OR 創業」，
        找媒體報導、政府補助入選名單、育成中心、加速器等第三方資訊。

步驟 2：若找到相關資訊，用 WebFetch 讀取最有參考價值的 1-2 篇文章全文。{known_hint}

完成搜尋後，輸出修訂版完整備忘錄（格式：## 業務概況、## 競業分析、## 主要風險）。
競業分析表格使用五欄：公司名稱 ｜ 核心業務 ｜ 主要差異化特點 ｜ 上市狀態 ｜ 競業類型（正面競業僅填有搜尋佐證者，1-3 家皆可；其餘三類各至少 1 家；**禁止捏造或推測公司名稱**）。

【競業表填寫規則（請遵循，但這些規則文字**禁止輸出到備忘錄裡**，只輸出填好的表格與分析）】
- 競業類型限填：正面競業／替代路徑／側翼潛入／垂直整合。
- 「公司名稱」欄一律填正式登記名稱（○○股份有限公司／○○有限公司）；只知品牌時請查出法人公司名，可在括號附品牌，禁止只填品牌或英文商標。
- 一列只填一家公司：不可在同一格用「、」「／」「與」把多家塞在一起，也不要加「上游」「下游」描述詞或「等」字；同類有多家就拆成多列。

若新資料提供了原版沒有的具體資訊，更新對應段落；若無新資訊，維持原內容。
**嚴格禁止在末尾輸出任何 Sources、References、來源清單或 URL 列表。**
**嚴格禁止把上面任何填寫規則／欄位說明文字複製進備忘錄內容。**

最後單獨一行，格式固定如下（不超過10個繁體中文字，描述核心產品或服務，禁止出現公司名稱）：
[blurb: 核心產品或服務描述]"""


_NORMAL_MODEL = "claude-sonnet-4-6"
# "opus" 是本機 claude CLI 的別名，會自動解析成當下最新的 Opus（目前 4.8）。
# 不寫死版本號，未來 Opus 迭代（4.9、5.0…）會自動跟上，無需再改。
_DEEP_MODEL = "opus"


def _grab_field(raw: str, label: str) -> str:
    """Extract the value after a「label：value」line from model output."""
    m = re.search(rf"^{re.escape(label)}\s*[：:]\s*(.+)$", raw or "", re.MULTILINE)
    return m.group(1).strip() if m else ""


async def analyze_competitor(company: dict, comp_name: str, comp_type: str,
                             engine: str = "claude") -> dict:
    """Research a single named competitor (WebSearch) in the context of the case
    company, and return {core_biz, differentiation, listing}. 競業類型 is supplied
    by the user, not the model."""
    name = company.get("name", "")
    short, full = _company_name_variants(name)
    biz = _extract_section(company.get("summary", ""), "業務概況") or company.get("blurb", "") or "不詳"
    prompt = f"""你是一位在台灣有豐富經驗的資深私募股權（PE）投資人。

本案公司是「{full}」（簡稱：{short}），其業務概況：
{biz[:700]}

請用 WebSearch 搜尋「{comp_name}」這家公司（官網、公司介紹、媒體報導、104 公司頁），
然後以「本案的競爭對手」角度分析它，回傳以下四項，**格式固定、每項一行、不要其他文字**：

正式登記名稱：（該公司的完整登記名稱，如 ○○股份有限公司／○○有限公司；查不到就填你查到的名稱）
核心業務：（一句話，{comp_name} 的核心產品或服務）
主要差異化特點：（相對本案，{comp_name} 的差異化或相對優劣勢，1-2 點，具體）
上市狀態：（只能填 上市／上櫃／興櫃／創新板／非公發 其一，查不到填 非公發）

若完全查無此公司資料，核心業務與差異化請據實標「——（查無公開資料）」。"""
    raw = await asyncio.to_thread(
        claude_client.ask, prompt, 180, _WEB_TOOLS, engine, 8, _NORMAL_MODEL
    )
    full_name = _grab_field(raw, "正式登記名稱") or comp_name
    listing_ai = _grab_field(raw, "上市狀態")
    listing_ai = listing_ai if listing_ai in _VALID_LISTING else "非公發"
    # Authoritative listing via TWSE/TPEX cache (matches on full legal name); fall
    # back to the model's WebSearch answer when the cache has no match.
    listing = listing_ai
    try:
        async with httpx.AsyncClient() as client:
            await _ensure_listing_cache(client)
        resolved = _resolve_listing_status("", full_name)
        if resolved in {"上市", "上櫃", "興櫃", "創新板"}:
            listing = resolved
    except Exception as e:
        log.warning("competitor listing resolve failed for %s: %s", full_name, e)
    return {
        "full_name": full_name,
        "core_biz": _grab_field(raw, "核心業務") or "——",
        "differentiation": _grab_field(raw, "主要差異化特點") or "——",
        "listing": listing,
    }


async def deep_enrich_summary(company: dict, engine: str = "claude",
                              competitor_context: dict | None = None) -> dict:
    """Search news/media and refine the existing summary. Returns {summary, blurb}.
    Uses the latest Opus (via the CLI "opus" alias) for higher-quality deep analysis."""
    name = company.get("name", "")
    prompt = _build_deep_prompt(company, competitor_context)
    model = _DEEP_MODEL
    async with _CLAUDE_LOCK:
        try:
            raw = await asyncio.to_thread(
                claude_client.ask, prompt, 480, _WEB_TOOLS, engine, 15, model
            )
            summary, blurb = _split_blurb(raw)
            if not blurb and len(summary.strip()) > 100:
                blurb = await _generate_blurb_fallback(summary, name, engine)
            async with httpx.AsyncClient() as client:
                await _ensure_listing_cache(client)
            summary = _fix_competitor_listing(summary)
            competitors = _parse_competitor_table(summary)
            return {"summary": summary, "blurb": blurb, "competitors": competitors}
        except Exception as e:
            raise RuntimeError(f"深度生成失敗：{e}") from e


_MEMO_LABELS = [
    ("deal_source", "案件來源"), ("interviewees", "受訪人"), ("paid_in_capital", "實收資本額"),
    ("address", "地址"), ("founding_date", "設立日期"), ("underwriter", "承銷商"),
    ("auditor", "會計師事務所"), ("chairman", "董事長"), ("general_manager", "總經理"),
    ("headcount", "員工人數"), ("ipo_timeline", "公開發行及上市櫃時程/募資規劃"),
    ("investment_terms", "增資計畫或投資條件"), ("business_revenue", "主要業務、產品營收比重"),
    ("financials", "財務狀況"), ("management_team", "經營團隊背景"),
    ("board_shareholding", "董監或主要股東持股情形"), ("recent_development", "公司發展近況"),
    ("major_customers", "主要銷貨客戶"), ("major_suppliers", "主要進貨廠商"),
    ("factory_capacity", "廠房及產能使用情形"), ("competitors", "國內外主要競爭對手"),
    ("industry_trends", "產業發展趨勢"), ("risk_tracking", "風險評估及追蹤事項"),
    ("conclusion", "評估結論與建議"),
]


def serialize_memo(memo: dict | None) -> str:
    """Turn a call_memo dict into a readable interview text block (non-empty fields)."""
    if not memo:
        return ""
    lines: list[str] = []
    date = (memo.get("interview_date") or "").strip()
    if date:
        lines.append(f"訪談日期：{date}")
    for key, label in _MEMO_LABELS:
        val = (memo.get(key) or "").strip()
        if val:
            lines.append(f"{label}：{val}")
    return "\n".join(lines)


def _extract_section(summary: str, heading: str) -> str:
    """Return the body text of a `## heading` section from a markdown summary, or ''."""
    out: list[str] = []
    capturing = False
    for line in (summary or "").split("\n"):
        s = line.strip()
        if re.match(r"^##\s+", s):
            if capturing:
                break
            capturing = (re.sub(r"^##\s+", "", s).strip() == heading)
            continue
        if capturing:
            out.append(line)
    return "\n".join(out).strip()


def _build_materials_prompt(company: dict, materials_text: str = "", interview_text: str = "") -> str:
    name     = company.get("name", "")
    industry = company.get("industry", "") or "不詳"
    rep      = company.get("representative", "") or "不詳"
    capital  = company.get("capital", 0)
    address  = company.get("address", "") or "不詳"
    listing  = company.get("listing_status", "非公發")
    tax_id   = company.get("tax_id", "")
    short_name, full_name = _company_name_variants(name)
    capital_str = f"NT$ {capital:,}" if capital else "不詳"

    text_block = (
        f"\n【上傳檔案文字內容（簡報／公司介紹／文件擷取）】\n{materials_text.strip()}\n"
        if materials_text.strip() else ""
    )
    has_interview = bool(interview_text.strip())
    interview_block = (
        f"\n【訪談備忘錄內容（來自實地訪談，由使用者填寫或逐字稿／錄音整理）】\n{interview_text.strip()}\n"
        if has_interview else ""
    )

    # How to tag supplements by source. With an interview present we distinguish
    # file-sourced「（簡報補充）」from interview-sourced「（訪談補充）」.
    _no_nest = (
        "【標記規則｜務必遵守】每一則補充只標記**一次**，二擇一：要嘛整段以標記開頭、"
        "要嘛行內以「（…：…）」附在敘述後。**嚴禁巢狀或重複**：已用「（簡報補充）」開頭的段落內,"
        "不可再出現任何「（簡報補充：…）」；同一句也不要疊兩個標記。"
    )
    if has_interview:
        src_tag = ("補充資訊請依**來源**標註：來自上傳檔案／簡報的標「（簡報補充）」、"
                   "來自訪談備忘錄的標「（訪談補充）」。" + _no_nest)
    else:
        src_tag = "簡報／檔案新增或更具體的內容請以「（簡報補充）」標示。" + _no_nest

    existing_biz = _extract_section(company.get("summary", ""), "業務概況")
    biz_block = (
        f"\n【目前公司簡介既有的「業務概況」（來自公開資料研究）】\n{existing_biz}\n"
        if existing_biz else ""
    )
    if existing_biz:
        biz_instruction = (
            "以上方「既有業務概況」為基礎，**完整保留**既有敘述；再融入上傳檔案或訪談中、"
            f"既有敘述沒有的額外資訊（產品、技術、客戶、市場）。{src_tag}"
            "不要刪除既有內容、不要原文重複既有敘述。"
        )
    else:
        biz_instruction = "公司在做什麼：主要產品或服務、核心技術、解決的問題"

    existing_risks = _extract_section(company.get("summary", ""), "主要風險")
    risk_block = (
        f"\n【目前公司簡介既有的「主要風險」（來自公開資料研究）】\n{existing_risks}\n"
        if existing_risks else ""
    )
    if existing_risks:
        risk_instruction = (
            "請先**完整保留**上方「既有主要風險」的每一條（可微調文字但不可刪除其指出的風險），"
            "再**補充**你從上傳檔案或訪談中發現、既有清單尚未涵蓋的額外風險。整合成一份去重後的完整清單："
            f"既有風險在前、新增風險在後，新增條目依來源於開頭標「（簡報補充）」或「（訪談補充）」。"
            "每條需具體指向本公司業務，禁止空泛語句。"
        )
    else:
        risk_instruction = (
            "以 PE 視角列 3-5 點具體風險，需具體指向本公司業務或產業，禁止「市場競爭激烈」「法規風險」等空泛語句。"
        )

    sources_desc = "上傳的簡報／公司介紹／照片等檔案" + ("，以及一份訪談備忘錄" if has_interview else "")

    return f"""你是一位在台灣有豐富經驗的資深私募股權（PE）投資人。
以下是「{full_name}」的補充資料（{sources_desc}），以及系統登記的基本資料。
請**完整閱讀所有已提供的內容**，僅根據這些補充資料與基本資料撰寫一份公司簡介。

【系統登記基本資料】
公司名稱：{full_name}（簡稱：{short_name}）
統一編號：{tax_id or "不詳"}
所屬產業：{industry}
代表人：{rep}
實收資本額：{capital_str}
所在地：{address}
上市狀態：{listing}
{text_block}{interview_block}{biz_block}{risk_block}
【任務】
用繁體中文撰寫以下格式的公司簡介（純 Markdown，不加開頭標題行）：

## 業務概況
（{biz_instruction}）

## 產品與服務
（具體產品線、服務項目、應用場景；可條列）

## 商業模式與市場
（如何賺錢、目標客戶、市場定位、競爭優勢；補充資料有提到才寫）

## 團隊與股東
（創辦人、經營團隊、重要股東或投資人；補充資料未提供則標「——（補充資料未提供）」）

## 財務與募資亮點
（營收、成長、募資金額、估值、訂單或客戶數等**具體數字**；補充資料未提供則標「——（補充資料未提供）」）

## 投資亮點
（以 PE 視角，列 2-4 點從補充資料讀到的投資亮點 / 重點；**只寫亮點，不要寫風險**，風險一律放到下面的「主要風險」）

## 主要風險
（{risk_instruction}）

通用規則：上面各段落中，凡是新增、補充或更具體的資訊，{src_tag}

嚴格規則：
- 各段落**只寫上傳檔案、訪談備忘錄或基本資料中明確出現的資訊**，不得引用外部知識或自行推測（例外：「業務概況」與「主要風險」需依上方指示整合既有內容）。
- 具體數字（營收、募資、估值、客戶數）務必依補充資料原文，**禁止杜撰、湊整或推估**。
- 某段落在補充資料中查無資訊時，直接標「——（補充資料未提供）」，不要用模糊語氣填充。
- 禁止描述自己的閱讀過程或檔案結構。
- **嚴格禁止在末尾輸出任何 Sources、References、來源清單或 URL 列表。**

最後單獨一行，格式固定如下（不超過10個繁體中文字，描述核心產品或服務，禁止出現公司名稱）：
[blurb: 核心產品或服務描述]"""


async def generate_summary_from_materials(
    company: dict, file_paths: list[str], materials_text: str = "",
    interview_text: str = "", engine: str = "claude",
) -> dict:
    """Scan uploaded supplementary material (slides/intro/photos + interview memo)
    with the latest Opus and produce a company profile that integrates them, tagging
    additions by source「（簡報補充）」/「（訪談補充）」.

    `file_paths` are binary-native files (PDF + images) read directly by the
    model; `materials_text` is pre-extracted text from office/txt files;
    `interview_text` is the serialized call-memo content."""
    name = company.get("name", "")
    prompt = _build_materials_prompt(company, materials_text, interview_text)
    model = _DEEP_MODEL
    async with _CLAUDE_LOCK:
        try:
            log.info("Generating materials summary for %s (%d files, interview=%s)",
                     name, len(file_paths), bool(interview_text.strip()))
            raw = await asyncio.to_thread(
                claude_client.ask_with_files, prompt, file_paths, 420, engine, model
            )
            summary, blurb = _split_blurb(raw)
            if not summary.strip():
                raise RuntimeError("模型未回傳任何內容")
            if not blurb and len(summary.strip()) > 100:
                blurb = await _generate_blurb_fallback(summary, name, engine)
            log.info("Materials summary done for %s (%d chars, blurb=%r)", name, len(summary), blurb)
            return {"summary": summary, "blurb": blurb}
        except Exception as e:
            raise RuntimeError(f"簡報生成失敗：{e}") from e


async def generate_summary(company: dict, engine: str = "claude",
                           competitor_context: dict | None = None) -> dict:
    """
    Returns a due-diligence memo in Traditional Chinese Markdown.
    Retries once on failure with a 5-second gap.
    """
    name = company.get("name", "")
    prompt = _build_prompt(company, competitor_context)
    model = _NORMAL_MODEL

    last_error: Exception | None = None
    async with _CLAUDE_LOCK:
        for attempt in range(2):
            try:
                log.info("Generating DD memo for %s (attempt %d)", name, attempt + 1)
                raw = await asyncio.to_thread(
                    claude_client.ask, prompt, 420, _WEB_TOOLS, engine, 12, model
                )
                summary, blurb = _split_blurb(raw)

                # Fallback: if blurb missing but summary has real content, generate quickly
                if not blurb and len(summary.strip()) > 100:
                    log.info("Blurb missing for %s, running fallback generation", name)
                    blurb = await _generate_blurb_fallback(summary, name, engine)

                # Correct AI-hallucinated listing status using TWSE/TPEX API
                async with httpx.AsyncClient() as client:
                    await _ensure_listing_cache(client)
                summary = _fix_competitor_listing(summary)
                competitors = _parse_competitor_table(summary)

                log.info("DD memo done for %s (%d chars, blurb=%r, competitors=%d)",
                         name, len(summary), blurb, len(competitors))
                return {"summary": summary, "blurb": blurb, "competitors": competitors}
            except Exception as e:
                last_error = e
                log.warning("DD memo attempt %d failed for %s: %s", attempt + 1, name, e)
                if attempt == 0:
                    await asyncio.sleep(5)

    # Both attempts failed — surface the underlying error so the SSE caller can
    # show it to the user instead of pretending generation succeeded.
    raise RuntimeError(f"公司簡介生成失敗：{last_error}") from last_error


async def _generate_blurb_fallback(summary: str, name: str, engine: str = "claude") -> str:
    """Quick fallback: generate ≤10-char blurb from existing summary text."""
    import re
    prompt = (
        f"請用不超過10個繁體中文字描述以下公司的核心產品或服務。"
        f"禁止出現公司名稱「{name}」。只輸出描述本身，不加標點、引號或說明文字。\n\n"
        f"{summary[:500]}"
    )
    try:
        raw = await asyncio.to_thread(claude_client.ask, prompt, 30, None, engine)
        blurb = raw.strip().split("\n")[0].strip()
        m = re.match(r'^\[blurb:\s*(.+?)\]\s*$', blurb)
        if m:
            blurb = m.group(1).strip()
        return blurb[:15]
    except Exception as e:
        log.warning("Blurb fallback failed for %s: %s", name, e)
        return ""


_VALID_LISTING = {"上市", "上櫃", "興櫃", "創新板", "非公發"}


_VALID_COMPETITION_TYPES = {"正面競業", "替代路徑", "側翼潛入", "垂直整合"}


def _parse_competitor_table(summary: str) -> list[dict]:
    """
    Extract structured competitor rows from the ## 競業分析 section.
    Supports both 4-column (legacy) and 5-column (with 競業類型) formats.
    Skips the header, separator, and 本案 rows.
    """
    competitors: list[dict] = []
    in_section = False

    for line in summary.split("\n"):
        s = line.strip()

        if re.match(r"^##\s+競業分析", s):
            in_section = True
            continue
        if in_section and re.match(r"^##\s+", s):
            break

        if not in_section or not (s.startswith("|") and s.endswith("|") and s.count("|") >= 5):
            continue

        cells = [c.strip() for c in s[1:-1].split("|")]
        if len(cells) < 4:
            continue
        if cells[0] == "公司名稱" or all(re.match(r"^-*$", c) for c in cells):
            continue
        if "（本案）" in cells[0]:
            continue

        name = cells[0]
        if not name:
            continue

        # 5-column: 公司名稱|核心業務|差異化|上市狀態|競業類型
        # 4-column (legacy): 公司名稱|核心業務|差異化|上市狀態
        if len(cells) >= 5 and cells[-1] in _VALID_COMPETITION_TYPES | {"—", "（AI 判斷填入）", ""}:
            listing = cells[-2] if cells[-2] in _VALID_LISTING else "非公發"
            competition_type = cells[-1] if cells[-1] in _VALID_COMPETITION_TYPES else ""
        else:
            listing = cells[-1] if cells[-1] in _VALID_LISTING else "非公發"
            competition_type = ""

        competitors.append({
            "name": name,
            "tax_id": None,
            "company_id": None,
            "core_biz": cells[1],
            "listing_status": listing,
            "competition_type": competition_type,
        })

    return competitors


def _fix_competitor_listing(text: str) -> str:
    """
    Scan competitor table rows and correct listing status via TWSE/TPEX cache.
    Supports both 4-column (legacy) and 5-column (with 競業類型) formats.
    Cache must be warm before calling (call _ensure_listing_cache first).
    """
    lines = text.split("\n")
    result = []
    for line in lines:
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|") and s.count("|") >= 5):
            result.append(line)
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if len(cells) < 4:
            result.append(line)
            continue

        # Detect format: 5-col has 競業類型 as last cell (not a listing value)
        if len(cells) >= 5 and cells[-1] not in _VALID_LISTING:
            listing_idx = len(cells) - 2  # 上市狀態 is second-to-last
        else:
            listing_idx = len(cells) - 1  # 上市狀態 is last (legacy)

        listing = cells[listing_idx]
        if listing not in _VALID_LISTING:
            result.append(line)
            continue

        company_name = cells[0].replace("（本案）", "").strip()
        if not company_name:
            result.append(line)
            continue

        resolved = _resolve_listing_status("", company_name)
        if resolved != listing:
            # Replace via pipe-split to handle any column position correctly
            parts = line.split("|")
            cell_pos = listing_idx + 1  # +1 because parts[0] is the leading ""
            if cell_pos < len(parts) and parts[cell_pos].strip() == listing:
                parts[cell_pos] = f" {resolved} "
                line = "|".join(parts)
            log.info("Corrected listing for %s: %s → %s", company_name, listing, resolved)
        result.append(line)
    return "\n".join(result)


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
