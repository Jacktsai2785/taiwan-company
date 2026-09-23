"""
Extract Call Memo fields from a meeting transcript using Claude CLI.
Returns a dict matching the 24-field template schema (+ interview_date on extraction).
"""
import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from services import claude_client

log = logging.getLogger("memo_extractor")

# Ordered field definitions: key -> (label, short_description)
FIELDS: list[tuple[str, str, str]] = [
    ("deal_source",        "案件來源",                   "例：自行開發、某人介紹；若未提及請留空"),
    ("interviewees",       "受訪人",                     "受訪者姓名與職稱，多人以頓號分隔"),
    ("paid_in_capital",    "實收資本額",                  "NT$ 金額，例：5,000萬"),
    ("address",            "地址",                       "公司登記地址或廠址"),
    ("founding_date",      "設立日期",                   "例：2018年 或 2018/03/01"),
    ("underwriter",        "承銷商",                     "輔導券商名稱，若未提及填空"),
    ("auditor",            "會計師事務所",                "簽證會計師事務所，若未提及填空"),
    ("chairman",           "董事長",                     "董事長姓名"),
    ("general_manager",    "總經理",                     "總經理姓名"),
    ("headcount",          "員工人數",                   "數字，例：120人"),
    ("ipo_timeline",       "公開發行及上市櫃時程/募資規劃", "IPO 目標年份、目前募資輪次等時間性資訊"),
    ("investment_terms",   "增資計畫或投資條件",          "本次募資總額、釋出股比、預計 close 時程"),
    ("business_revenue",   "主要業務、產品營收比重",       "核心業務說明及各產品/服務的營收佔比"),
    ("tech_description",   "公司技術說明",                "核心技術、技術門檻、專利或研發能力等說明"),
    ("financials",         "財務狀況",                   "近期營收、淨利、年增率等財務數據"),
    ("management_team",    "經營團隊背景",                "創辦人/CEO、CTO、CFO 的背景與經歷"),
    ("board_shareholding", "董監或主要股東持股情形",       "主要股東名稱與持股比例"),
    ("recent_development", "公司發展近況",                "近期重大里程碑、產品進展、合作案"),
    ("major_customers",    "主要銷貨客戶",                "前幾大客戶名稱與佔比"),
    ("major_suppliers",    "主要進貨廠商",                "主要原物料或零件供應商"),
    ("factory_capacity",   "廠房及產能使用情形",          "廠房地點、產能規模、目前使用率"),
    ("competitors",        "國內外主要競爭對手",          "直接競爭者名稱及差異化分析"),
    ("industry_trends",    "產業發展趨勢",                "產業現況、市場規模、未來展望"),
    ("memo_notes",         "Memo",                       "使用者自由備註，不由 AI 自動抽取"),
    ("risk_tracking",      "風險評估及追蹤事項",          "主要風險點與需持續追蹤的議題"),
    ("conclusion",         "評估結論與建議",              "訪談整體評估與後續建議行動"),
]

FIELD_KEYS = [f[0] for f in FIELDS]

# 財務狀況欄位在 DOCX 範本裡有一張巢狀表格（年度 x Now/Now+1/Now+2/Now+3），跟
# financials 的自由文字是同一儲存格裡的兩個部分：文字給敘述，這張表給結構化數字。
# 不併進 FIELDS——FIELDS 是單一欄位單一自由文字的清單，這張表是 16 指標 x 4 期
# 的固定格狀資料，key 用 fin_<period>_<metric> 展平儲存，方便沿用既有的
# MemoSave / _new_memo_entry 平面 key-value 模式。
# 順序、標籤要跟 data/call_memo_template.docx 巢狀表格的列標籤逐字對應
# （fill_template 靠標籤文字比對填值）——改這裡要同步改範本，或反過來。
# 第三個欄位是常見會計科目同義詞，餵給 _synthesize_financials_table 的 prompt，
# 讓 AI 認得補充資料裡不同措辭都對應到同一個 metric（例如「銷貨成本」＝COGS）——
# 這是讀寫這張表時辨識科目名稱的唯一基準，之後要擴充同義詞改這裡就好。
FINANCIAL_TABLE_METRICS: list[tuple[str, str, str]] = [
    ("revenue",               "營收",      "營業收入淨額、收入、營收淨額"),
    ("cogs",                  "COGS",      "銷貨成本、營業成本"),
    ("production_headcount",  "生產人數",   "產線/生產/製造部門人數"),
    ("gross_profit",          "毛利",      "營業毛利、銷貨毛利"),
    ("gross_margin_pct",      "毛利率(%)", "毛利率"),
    ("expenses",              "營業費用",   "營業費用總額"),
    ("selling",               "Selling",   "推銷費用、銷售費用"),
    ("sales_headcount",       "Sales人數",  "銷售/業務部門人數"),
    ("ga",                    "G&A",       "管理費用、一般管理費用"),
    ("ga_headcount",          "G&A人數",    "管理部門人數"),
    ("rd",                    "R&D",       "研究發展費用、研發費用"),
    ("rd_headcount",          "RD人數",    "研發部門人數"),
    ("operating_income",      "營業利益",   "營業淨利"),
    ("non_operating_income",  "業外收入",   "營業外收入"),
    ("non_operating_expense", "業外支出",   "營業外支出"),
    ("net_income",            "稅後淨利",   "本期淨利、稅後純益"),
]
FINANCIAL_TABLE_PERIODS: list[tuple[str, str]] = [
    ("now",  "Now"),
    ("now1", "Now+1"),
    ("now2", "Now+2"),
    ("now3", "Now+3"),
]
FINANCIAL_TABLE_KEYS = [
    f"fin_{period_key}_{metric_key}"
    for period_key, _ in FINANCIAL_TABLE_PERIODS
    for metric_key, _, _ in FINANCIAL_TABLE_METRICS
]
# 每期的實際期間/截止日期（例如「114/8/31」），跟數字分開存，讓表頭能顯示補充
# 資料裡真正的財報期間，而不是永遠只顯示 Now/Now+1 這種通用代稱。
FINANCIAL_PERIOD_LABEL_KEYS = [
    f"fin_period_label_{period_key}" for period_key, _ in FINANCIAL_TABLE_PERIODS
]

# 自由備註欄位：不參與 AI 逐字稿抽取／統整，只走 MemoSave / DOCX / serialize_memo。
_MANUAL_ONLY_KEYS = {"memo_notes"}

# 抽取時額外請 AI 從逐字稿判斷訪談日期（不併進 FIELDS——FIELDS 是 memo 正文欄位的
# 唯一真理來源，供 MemoSave / DOCX 範本 / serialize_memo 共用；interview_date 另走流程）。
# _MANUAL_ONLY_KEYS（如 memo_notes）不參與抽取，避免浪費 AI 額度也避免覆蓋使用者手寫備註。
_EXTRACT_FIELDS = [("interview_date", "訪談日期", "逐字稿中提到的訪談/會議日期，格式 YYYY/MM/DD；未提及請留空")] + [
    f for f in FIELDS if f[0] not in _MANUAL_ONLY_KEYS
]
_EXTRACT_KEYS = [f[0] for f in _EXTRACT_FIELDS]

_CHUNK_CHARS = 12000
_CHUNK_OVERLAP = 500
PROMPT_VERSION = "evidence-v6.1-synthesized"

# Keep each synthesis prompt focused enough that the model can actually edit
# prose instead of falling back to copying a long evidence dump.  Evidence is
# still stored in full in the audit file; these groups only control the final
# presentation pass.
_SYNTHESIS_GROUPS: tuple[tuple[str, ...], ...] = (
    (
        "deal_source", "interviewees", "paid_in_capital", "address",
        "founding_date", "underwriter", "auditor", "chairman",
        "general_manager", "headcount",
    ),
    (
        "ipo_timeline", "investment_terms", "financials",
        "board_shareholding",
    ),
    (
        "business_revenue", "tech_description", "recent_development",
        "major_customers", "major_suppliers", "factory_capacity",
    ),
    (
        "management_team", "competitors", "industry_trends",
        "risk_tracking", "conclusion",
    ),
)

_QUESTION_CUES = re.compile(r"(?:有沒有|有没有|是不是|會不會|会不会|你覺得|你觉得|嗎|吗|[?？])(?:[。.!！]?)$")
_THIRD_PARTY_EXAMPLE_CUES = re.compile(r"(?:比如|例如|假設|假设|uber|wechat|特斯拉)", re.I)
_FIRST_PARTY_CUES = re.compile(r"(?:我們|我们|本公司|我司|本團隊|本团队)")

# Generic investment-signal grammar. No company-, year-, amount-, or country-
# specific literals belong here.
_GENERIC_SIGNAL_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("financials", re.compile(
        r"(?:nt\$|us\$|rmb|jpy|usd|twd|新台幣|人民幣|美元|日圓|日元)\s*"
        r"(?:\d[\d,.]*|[一二三四五六七八九十百千兩两]+)(?:萬|万|億|亿|\s*(?:k|m|million|billion))?|"
        r"(?:\d[\d,.]*|[一二三四五六七八九十百千兩两]+)"
        r"(?:(?:萬|万|億|亿)(?:元|塊|美元|日圓|日元)?|(?:元|塊|美元|日圓|日元)|\s*(?:k|m|million|billion)\b)|"
        r"(?:\d+(?:\.\d+)?\s*%|百分之[一-鿿\d.]+)|"
        r"(?:營收|营收|淨利|净利|毛利|獲利|获利|成長率|成长率|複合成長|复合成长)")),
    ("headcount", re.compile(r"(?:\d[\d,]*|[一二三四五六七八九十百千兩两]+)\s*(?:個|个)?(?:人|名)(?:員工|员工|團隊|团队|工程師|工程师|研發|研发|rd)?", re.I)),
    ("recent_development", re.compile(r"(?:19|20)\d{2}(?:年)?|\bhr\b|行政|財務|财务|組織調整|组织调整|里程碑", re.I)),
    ("major_customers", re.compile(r"客戶|客户|市場|海外|出口|跨境|國際|国际|全球")),
    ("management_team", re.compile(r"核心能力|關鍵人員|关键人员|keyman|創辦人|创办人|執行長|执行长|ceo|cto|cfo", re.I)),
    ("risk_tracking", re.compile(r"風險|风险|商機|商机|下降|流失|離職|离职|法規|現金流|现金流|中斷|取代|不承接|不接")),
    ("factory_capacity", re.compile(r"產能|产能|良率|廠房|厂房|稼動率|稼动率|月產|月产")),
    ("ipo_timeline", re.compile(r"\bipo\b|公開發行|公开发行|上市|上櫃|上柜|興櫃|兴柜", re.I)),
    ("investment_terms", re.compile(r"投資人|投资人|募資|募资|增資|增资|估值|併購|并购|股權|股权|表決|表决|釋股|释股|close", re.I)),
)


def prepare_transcript(transcript: str, source_filename: str = "") -> str:
    """Remove generated Markdown summaries/metadata when a raw transcript section exists."""
    if Path(source_filename).suffix.lower() != ".md":
        return transcript
    marker = re.search(r"(?im)^##\s*逐字稿\s*$", transcript)
    return transcript[marker.end():].lstrip() if marker else transcript


def infer_date_from_filename(source_filename: str) -> str:
    """Infer YYYY/MM/DD from an explicit date token in a filename."""
    stem = Path(source_filename).stem
    for pattern in (
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",
        r"(?<!\d)(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})(?!\d)",
    ):
        match = re.search(pattern, stem)
        if not match:
            continue
        candidate = "/".join(match.groups())
        try:
            return datetime.strptime(candidate, "%Y/%m/%d").strftime("%Y/%m/%d")
        except ValueError:
            continue
    return ""


def _split_transcript(transcript: str) -> list[str]:
    """超長逐字稿切成有重疊的片段，避免在句子中間切斷漏抽。"""
    if len(transcript) <= _CHUNK_CHARS:
        return [transcript]
    chunks, start, step = [], 0, _CHUNK_CHARS - _CHUNK_OVERLAP
    while start < len(transcript):
        chunks.append(transcript[start:start + _CHUNK_CHARS])
        start += step
    return chunks


async def extract_from_transcript(
    company_name: str,
    transcript: str,
    engine: str = "",
    source_filename: str = "",
) -> dict:
    fields, _audit = await extract_with_audit(
        company_name, transcript, engine=engine, source_filename=source_filename
    )
    return fields


async def extract_with_audit(
    company_name: str,
    transcript: str,
    engine: str = "",
    source_filename: str = "",
    native_file_path: str = "",
) -> tuple[dict, dict]:
    """
    從逐字稿抽取所有 Call Memo 欄位（+ interview_date）。
    逐字稿超長時分段抽取「事實 + 原文證據」，累積去重後再統整成 24 欄。
    AI 回不出可解析 JSON 時 raise ValueError（不偽裝成 24 欄全空的『成功』）。

    native_file_path：原始檔案（PDF/圖片）路徑。get_text() 抽出的 transcript 只有文字層，
    設計排版頁（封面、經營團隊、組織圖等常見版面）會整頁抓成空字串；有給路徑時另外讓模型
    原生讀圖補一份 evidence，跟文字抽取結果併入同一個 evidence pool（見 _extract_evidence_from_file）。
    """
    transcript = prepare_transcript(transcript, source_filename)
    chunks = _split_transcript(transcript)
    evidence: dict[str, list[dict[str, str]]] = {k: [] for k in _EXTRACT_KEYS}
    chunk_results = await asyncio.gather(*(
        _extract_chunk_dual(company_name, chunk, engine) for chunk in chunks
    ))
    candidate_chunks = _split_transcript(_material_candidate_text(transcript))
    candidate_raw = await asyncio.gather(*(
        _extract_material_facts_once(company_name, chunk, engine)
        for chunk in candidate_chunks if chunk.strip()
    ), return_exceptions=True)
    candidate_results = [result for result in candidate_raw if isinstance(result, dict)]
    chunk_results.extend([[result] for result in candidate_results])
    deterministic = _deterministic_material_evidence(transcript)
    for items in deterministic.values():
        for item in items:
            item["source"] = "deterministic_safety_net"
    chunk_results.append([deterministic])
    if native_file_path:
        vision_results = await _extract_evidence_dual_from_file(company_name, native_file_path, engine)
        chunk_results.append(vision_results)
    for parts in chunk_results:
        for part in parts:
            for key, items in part.items():
                seen = {(e["fact"], e["quote"], e["timestamp"]) for e in evidence[key]}
                for item in items:
                    anchor = _normalized_text(str(item.get("anchor_quote") or ""))
                    if anchor and any(
                        anchor in _normalized_text(existing["quote"])
                        for existing in evidence[key]
                    ):
                        continue
                    identity = (item["fact"], item["quote"], item["timestamp"])
                    if identity not in seen:
                        evidence[key].append(item)
                        seen.add(identity)

    evidence_by_id: dict[str, list[dict[str, str]]] = {}
    for key in _EXTRACT_KEYS:
        evidence_by_id[key] = [
            {**item, "id": f"{key}:{index}"}
            for index, item in enumerate(evidence[key], start=1)
        ]

    fields, coverage = await _synthesize_fields_from_evidence(
        company_name, evidence_by_id, engine
    )
    if not fields["interview_date"]:
        fields["interview_date"] = infer_date_from_filename(source_filename)
    return fields, {
        "evidence": evidence_by_id,
        "coverage": coverage,
    }


async def _extract_chunk_dual(
    company_name: str, transcript: str, engine: str
) -> list[dict]:
    """Run complementary extractors; one timeout must not discard the other."""
    results = await asyncio.gather(
        _extract_evidence_once(company_name, transcript, engine),
        _extract_material_facts_once(company_name, transcript, engine),
        return_exceptions=True,
    )
    successful = [result for result in results if isinstance(result, dict)]
    if successful:
        return successful
    first_error = next((result for result in results if isinstance(result, Exception)), None)
    raise ValueError(f"AI 無法完成逐字稿分段抽取：{first_error}")


def _json_object(raw: str) -> dict:
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"\n?```$", "", raw.strip(), flags=re.MULTILINE)
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("AI 未回傳可解析的 call memo（請重試或換引擎）")
    try:
        data = json.loads(match.group())
    except Exception as e:
        raise ValueError("AI 回傳的 call memo 非合法 JSON（請重試或換引擎）") from e
    if not isinstance(data, dict):
        raise ValueError("AI 回傳的 call memo 不是 JSON 物件（請重試或換引擎）")
    return data


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _material_candidate_text(transcript: str) -> str:
    """Select high-signal transcript windows for a focused omission audit."""
    lines = transcript.splitlines()
    selected: set[int] = set()
    for index, line in enumerate(lines):
        content = re.sub(r"^\s*\[\d{2}:\d{2}:\d{2}\]\s*", "", line).lower()
        if any(pattern.search(content) for _, pattern in _GENERIC_SIGNAL_RULES):
            selected.update(range(max(0, index - 2), min(len(lines), index + 3)))
    return "\n".join(lines[index] for index in sorted(selected))


def _deterministic_material_evidence(transcript: str) -> dict:
    """Last-resort verbatim capture for facts models commonly omit."""
    lines = transcript.splitlines()
    result: dict[str, list[dict[str, str]]] = {key: [] for key in _EXTRACT_KEYS}
    seen: set[tuple[str, int, int]] = set()
    for index, line in enumerate(lines):
        content = re.sub(r"^\s*\[\d{2}:\d{2}:\d{2}\]\s*", "", line)
        for field, pattern in _GENERIC_SIGNAL_RULES:
            if not pattern.search(content):
                continue
            window_text = "\n".join(lines[max(0, index - 2):min(len(lines), index + 3)])
            # Questions and unrelated examples are candidates for AI review, but
            # are unsafe to force directly into the final memo.
            if _QUESTION_CUES.search(content):
                continue
            if _THIRD_PARTY_EXAMPLE_CUES.search(window_text) and not _FIRST_PARTY_CUES.search(window_text):
                continue
            start, end = max(0, index - 2), min(len(lines), index + 3)
            identity = (field, start, end)
            if identity in seen:
                continue
            seen.add(identity)
            quote = "\n".join(lines[start:end]).strip()
            # Keep surrounding lines as audit evidence, but only render the
            # matched assertion as the fact. This avoids turning context and
            # interviewer chatter into memo prose.
            fact = content.strip()
            timestamp_match = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", line)
            result[field].append({
                "fact": fact,
                "quote": quote,
                "timestamp": timestamp_match.group(1) if timestamp_match else "",
                "anchor_quote": line.strip(),
            })
    return result


async def _extract_evidence_once(company_name: str, transcript: str, engine: str) -> dict:
    fields_desc = "\n".join(
        f'  "{key}": "{label}（{desc}）"'
        for key, label, desc in _EXTRACT_FIELDS
    )

    prompt = f"""你是逐字稿事實抽取器。以下是與「{company_name}」的訪談逐字稿其中一段。

請為每個欄位找出所有明確事實與逐字證據，以 JSON 回傳：
- 每項格式為 {{"fact":"繁體中文事實", "quote":"原文逐字引用", "timestamp":"HH:MM:SS"}}。
- quote 必須逐字複製本段內容，不得翻譯、改寫或修正簡繁體；找不到原文引用就不要輸出。
- fact 只能表達 quote 直接支持的內容，不得加入常識、推測、評價或外部資訊。
- 同一事實可放入多個真正相關的欄位，以免後續漏掉重要內容。
- 這是高召回率抽取，不是摘要；寧可多列也不可只挑幾個重點。
- 逐項檢查：金額與比例、年份與時程、人數、客戶地區與類型、產品與收入模式、核心能力、經營原則、明確不做的事、風險、轉折點、海外布局、募資與 IPO，有提及都要列入。
- 本段未提及的欄位回傳空陣列 []。
- generated_at、檔案建立時間及逐字稿產生時間不是訪談日期。
- 回傳純 JSON，不要加 markdown code block 或說明。

需提取的欄位：
{{
{fields_desc}
}}

逐字稿內容：
---
{transcript}
---

請直接回傳 JSON 物件，每個 key 的 value 都是陣列。"""

    raw = await asyncio.to_thread(claude_client.ask, prompt, 180, None, engine)
    data = _json_object(raw)
    return _validated_evidence(data, transcript)


_LIST_RECALL_RULE = (
    "- 若某頁是名冊、清單或並列項目（例如經營團隊每個人的介紹、董監事名單、客戶清單），"
    "必須把清單上的每一筆都各自輸出成一條 fact，不可只挑幾筆當代表或摘要整體、"
    "也不可把多筆合併成一條。清單有幾筆就要輸出幾筆。"
)
_ACCURACY_RULE = (
    "- 人名、學校、公司等專有名詞如果圖片模糊、筆畫看不清楚，"
    "寧可整條不要輸出，也不可以猜測、改寫或用常見的相似名字替代。"
    "找不到、看不清楚就是留白，不是用合理猜測填上去。"
)


def _vision_result_from_data(data: dict, source: str) -> dict:
    result: dict[str, list[dict[str, str]]] = {k: [] for k in _EXTRACT_KEYS}
    for key in _EXTRACT_KEYS:
        items = data.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            fact = str(item.get("fact") or "").strip()
            if not fact:
                continue
            result[key].append({
                "fact": fact,
                "quote": str(item.get("quote") or "").strip(),
                "timestamp": str(item.get("timestamp") or "").strip(),
                "source": source,
            })
    return result


async def _extract_evidence_from_file(company_name: str, file_paths: list[str], engine: str) -> dict:
    """讓模型原生讀取原始檔案（PDF 頁面圖片或圖片檔），補足 get_text() 抓不到的設計
    排版頁內容（封面、經營團隊、組織圖等常見版面）。無逐字稿原文可比對，故無法套用
    _validated_evidence 的引用比對防幻覺機制，改以 vision_extraction 標記讓稽核
    可區分來源可信度，並在 prompt 內強調只能回報實際看到的內容。"""
    fields_desc = "\n".join(
        f'  "{key}": "{label}（{desc}）"'
        for key, label, desc in _EXTRACT_FIELDS
    )
    prompt = f"""你是投資文件事實抽取器。附件是與「{company_name}」相關的補充資料原始檔案，
可能包含設計排版頁面（封面、經營團隊介紹、組織圖等），也可能有純文字或表格頁面。
請完整讀取每一頁／每張圖片後再回答，不要只看檔名。

請為每個欄位找出所有明確事實，以 JSON 回傳：
- 每項格式為 {{"fact":"繁體中文事實", "quote":"檔案中可見的原文文字或數字（找不到就留空字串）", "timestamp":""}}。
- fact 只能表達你在檔案中實際看到的內容，不得加入常識、推測、評價或外部資訊。
- 同一事實可放入多個真正相關的欄位，以免後續漏掉重要內容。
- 這是高召回率抽取，不是摘要；寧可多列也不可只挑幾個重點。
{_LIST_RECALL_RULE}
{_ACCURACY_RULE}
- 檔案中未出現的欄位回傳空陣列 []。
- 回傳純 JSON，不要加 markdown code block 或說明。

需提取的欄位：
{{
{fields_desc}
}}

請直接回傳 JSON 物件，每個 key 的 value 都是陣列。"""

    raw = await asyncio.to_thread(
        claude_client.ask_with_files, prompt, file_paths, 240, engine
    )
    data = _json_object(raw)
    return _vision_result_from_data(data, "vision_extraction")


async def _extract_material_facts_from_file(company_name: str, file_paths: list[str], engine: str) -> dict:
    """跟 _extract_evidence_from_file 獨立、互補的第二條讀圖抽取——比照文字管線
    的 _extract_chunk_dual 雙抽取設計：單一 pass 讀密集名冊/清單頁容易漏，兩條
    獨立 pass 用不同的提問角度各自掃一次、結果合併，任一條漏掉的另一條有機會補上。"""
    field_choices = "\n".join(
        f'- {key}: {label}' for key, label, _ in _EXTRACT_FIELDS
    )
    prompt = f"""你是投資文件的高召回率事實稽核員。附件是與「{company_name}」相關的補充資料原始
檔案，請完整讀取每一頁／每張圖片，按頁面出現順序，掃描每一個投資重要事實。

這不是摘要。特別不可漏掉：
- 金額、比例、人數、年份、時程與成長率
- 經營團隊、董監事、關鍵人員：每個人的姓名、學歷、職稱、經歷都要各自列一條，不可只列幾位代表
- 客戶地區與類型、訂單變化、收入模式與海外布局
- 核心能力、組織變化、經營原則與明確不做的事
- 募資、併購、股權、表決權、IPO 動機與資金用途
{_LIST_RECALL_RULE}
{_ACCURACY_RULE}

每筆事實指定一個最適合的 field，格式：
{{"facts":[{{"field":"field_key","fact":"繁體中文事實","quote":"檔案中可見的原文文字（找不到就留空字串）","timestamp":""}}]}}

field 只能從以下選擇：
{field_choices}

只回傳純 JSON，不要加 markdown code block 或說明。"""

    raw = await asyncio.to_thread(
        claude_client.ask_with_files, prompt, file_paths, 240, engine
    )
    data = _json_object(raw)
    grouped: dict[str, list[dict]] = {key: [] for key in _EXTRACT_KEYS}
    for item in data.get("facts", []):
        if isinstance(item, dict) and item.get("field") in grouped:
            grouped[item["field"]].append(item)
    return _vision_result_from_data(grouped, "vision_extraction")


def _prepare_vision_files(native_file_path: str) -> tuple[list[str], list[str]]:
    """回傳 (要交給模型讀的檔案路徑清單, 用完要清掉的暫存檔清單)。

    直接把整份 PDF 交給 CLI 自己的 Read tool 讀，遇到超長版面（例如整頁截圖式
    排版）CLI 內部轉圖片這一步畫質常常被壓得比原始內嵌圖片差，模型會把相似字形
    猜成別的字（幻覺人名/學校，而不是單純漏抓）。改成我們自己用
    file_parser.extract_pdf_image_only_pages 把「沒有文字層的那幾頁」內嵌的原始
    圖片直接抓出來，原封不動交給模型——畫質等於人眼直接看到的版面。
    非 PDF（單張圖片檔）或抓不到任何圖片頁的 PDF，直接交回原始路徑。"""
    if Path(native_file_path).suffix.lower() != ".pdf":
        return [native_file_path], []
    from services import file_parser
    try:
        content = Path(native_file_path).read_bytes()
    except OSError:
        return [native_file_path], []
    page_images = file_parser.extract_pdf_image_only_pages(content)
    if not page_images:
        return [native_file_path], []
    tmp_paths: list[str] = []
    for i, (ext, img_bytes) in enumerate(page_images):
        suffix = f"_p{i + 1}.{ext or 'png'}"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(img_bytes)
        tmp.close()
        tmp_paths.append(tmp.name)
    return tmp_paths, tmp_paths


async def _extract_evidence_dual_from_file(company_name: str, native_file_path: str, engine: str) -> list[dict]:
    """比照 _extract_chunk_dual：兩條獨立讀圖 pass 一起跑，其中一條失敗或漏抓
    不影響另一條，結果都合併進同一個 evidence pool。"""
    file_paths, tmp_paths = _prepare_vision_files(native_file_path)
    try:
        results = await asyncio.gather(
            _extract_evidence_from_file(company_name, file_paths, engine),
            _extract_material_facts_from_file(company_name, file_paths, engine),
            return_exceptions=True,
        )
        successful = [result for result in results if isinstance(result, dict)]
        for result in results:
            if isinstance(result, Exception):
                log.warning("原始檔案 vision 抽取其中一條 pass 失敗（%s）：%s", native_file_path, result)
        return successful
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass


async def _extract_material_facts_once(
    company_name: str, transcript: str, engine: str
) -> dict:
    field_choices = "\n".join(
        f'- {key}: {label}' for key, label, _ in _EXTRACT_FIELDS
    )
    prompt = f"""你是投資訪談的高召回率事實稽核員。請按原文出現順序，掃描「{company_name}」這段逐字稿的每個投資重要事實。

這不是摘要。特別不可漏掉：
- 金額、比例、人數、年份、時程與成長率
- 客戶地區與類型、訂單變化、收入模式與海外布局
- 核心能力、組織變化、關鍵人員、經營原則與明確不做的事
- 疫情、AI、人才、客戶、競爭、法規、現金流等風險
- 募資、併購、股權、表決權、IPO 動機與資金用途

每筆事實指定一個最適合的 field，格式：
{{"facts":[{{"field":"field_key","fact":"繁體中文事實","quote":"原文逐字引用","timestamp":"HH:MM:SS"}}]}}

field 只能從以下選擇：
{field_choices}

quote 必須逐字存在於本段原文；無法提供引用就不得輸出。只回傳純 JSON。

逐字稿：
---
{transcript}
---
"""
    raw = await asyncio.to_thread(claude_client.ask, prompt, 180, None, engine)
    data = _json_object(raw)
    grouped: dict[str, list[dict]] = {key: [] for key in _EXTRACT_KEYS}
    for item in data.get("facts", []):
        if isinstance(item, dict) and item.get("field") in grouped:
            grouped[item["field"]].append(item)
    return _validated_evidence(grouped, transcript)


def _validated_evidence(data: dict, transcript: str) -> dict:
    normalized_chunk = _normalized_text(transcript)
    result: dict[str, list[dict[str, str]]] = {k: [] for k in _EXTRACT_KEYS}
    for key in _EXTRACT_KEYS:
        items = data.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            fact = str(item.get("fact") or "").strip()
            quote = str(item.get("quote") or "").strip()
            timestamp = str(item.get("timestamp") or "").strip()
            # Hard hallucination gate: the cited quote must exist verbatim in this chunk.
            if fact and quote and _normalized_text(quote) in normalized_chunk:
                result[key].append({"fact": fact, "quote": quote, "timestamp": timestamp})
    return result


def _deduplicated_facts(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Select readable evidence and remove exact/containment duplicates.

    Verbatim safety-net captures are always audit-only. They help reviewers find
    possible omissions, but lack the semantic judgment required for publication
    (for example, a third-party factory example can mention "產能").
    """
    selected: list[dict[str, str]] = []
    normalized_facts: list[str] = []
    model_items = [
        item for item in items
        if item.get("source") != "deterministic_safety_net"
    ]
    for item in model_items:
        fact = str(item.get("fact") or "").strip()
        normalized = _normalized_text(fact)
        if not normalized:
            continue
        contained_at = next(
            (i for i, existing in enumerate(normalized_facts)
             if normalized in existing or existing in normalized),
            None,
        )
        if contained_at is None:
            selected.append(item)
            normalized_facts.append(normalized)
        elif len(normalized) > len(normalized_facts[contained_at]):
            selected[contained_at] = item
            normalized_facts[contained_at] = normalized
    return selected


def _fallback_field_text(items: list[dict[str, str]], limit: int = 8) -> str:
    """Readable non-AI fallback: no duplicated sentence terminators or raw flood.
    多筆事實時比照 _synthesize_field_group 的條列規則，一筆一行、行首加「• 」，
    跟正常統整路徑的呈現方式一致；只有一筆事實就維持單一段落。"""
    facts = []
    for item in _deduplicated_facts(items):
        fact = str(item.get("fact") or "").strip().rstrip("。；; ")
        if fact:
            facts.append(fact)
        if len(facts) >= limit:
            break
    if not facts:
        return ""
    if len(facts) == 1:
        return facts[0] + "。"
    return "\n".join(f"• {fact}。" for fact in facts)


def _interview_date_from_evidence(items: list[dict[str, str]]) -> str:
    for item in items:
        fact = str(item.get("fact") or "")
        match = re.search(r"\b(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\b", fact)
        if not match:
            continue
        try:
            return datetime.strptime(
                "/".join(match.groups()), "%Y/%m/%d"
            ).strftime("%Y/%m/%d")
        except ValueError:
            continue
    return ""


async def _synthesize_fields_from_evidence(
    company_name: str,
    evidence: dict[str, list[dict[str, str]]],
    engine: str,
) -> tuple[dict[str, str], dict[str, dict]]:
    """Turn verified evidence into concise, readable Call Memo prose.

    The model may select and merge evidence, but every non-empty field must cite
    at least one valid evidence ID. Raw evidence remains in the audit payload.
    """
    fields = {key: "" for key in _EXTRACT_KEYS}
    fields.update({key: "" for key in FINANCIAL_TABLE_KEYS})
    coverage: dict[str, dict] = {}
    fields["interview_date"] = _interview_date_from_evidence(
        evidence["interview_date"]
    )
    coverage["interview_date"] = {
        "evidence_count": len(evidence["interview_date"]),
        "used_count": 1 if fields["interview_date"] else 0,
        "synthesized": False,
    }

    group_results, financials_table = await asyncio.gather(
        asyncio.gather(*(
            _synthesize_field_group(company_name, group, evidence, engine)
            for group in _SYNTHESIS_GROUPS
        ), return_exceptions=True),
        _synthesize_financials_table(company_name, evidence["financials"], engine),
    )
    fields.update(financials_table)
    results = group_results

    for group, result in zip(_SYNTHESIS_GROUPS, results):
        parsed = result if isinstance(result, dict) else {}
        for key in group:
            items = evidence[key]
            valid_ids = {item["id"] for item in items}
            entry = parsed.get(key) if isinstance(parsed, dict) else None
            text = ""
            used_ids: list[str] = []
            if isinstance(entry, dict):
                candidate = str(entry.get("text") or "").strip()
                cited = entry.get("evidence_ids") or []
                if isinstance(cited, list):
                    used_ids = [str(i) for i in cited if str(i) in valid_ids]
                # A non-empty answer without a real evidence citation is not
                # trusted. Fall back to bounded deterministic prose instead.
                if candidate and used_ids:
                    text = _normalize_memo_prose(candidate)
            if not text and items:
                text = _fallback_field_text(items)
            fields[key] = text
            coverage[key] = {
                "evidence_count": len(items),
                "used_count": len(set(used_ids)),
                "synthesized": bool(text and used_ids),
            }
    return fields, coverage


_CURRENCY_UNIT_SUFFIX_RE = re.compile(r"元$")


def _strip_currency_unit(value: str) -> str:
    """表格是數字欄位，不需要「元」這種單位字樣（單位在表頭/欄位名稱已經講明）。
    只去掉結尾的「元」，不動百分號（毛利率(%) 那格需要保留 %）、不重新換算
    金額大小——萬元/億元換算成基本元是 prompt 裡就要求 AI 做的事，這裡只是防呆，
    去掉結尾「元」已經同時涵蓋「萬元」「億元」這類複合單位。"""
    return _CURRENCY_UNIT_SUFFIX_RE.sub("", value.strip()).strip()


async def _synthesize_financials_table(
    company_name: str,
    financials_evidence: list[dict[str, str]],
    engine: str,
) -> dict[str, str]:
    """從 financials 欄位已驗證的事實裡，挑出能對應到多期財務表格（年度 x
    Now/Now+1/Now+2/Now+3）的數字，自動填進 FINANCIAL_TABLE_KEYS，並嘗試判斷
    每期實際代表的財報期間（FINANCIAL_PERIOD_LABEL_KEYS，例如「114/8/31」）。
    不確定或補充資料未提供的期別/指標留空，讓使用者在 UI 手動補（大多數上傳
    資料只有當期實際數字，Now+1~+3 通常是未來預測，本來就不是能從公司資料
    抽出來的東西）。抽取失敗時整表留空，不影響其他欄位的統整結果。"""
    result = {key: "" for key in FINANCIAL_TABLE_KEYS}
    result.update({key: "" for key in FINANCIAL_PERIOD_LABEL_KEYS})
    facts = _deduplicated_facts(financials_evidence)
    if not facts:
        return result

    metrics_desc = "\n".join(
        f'- {key}: {label}（常見措辭：{hint}）' if hint else f'- {key}: {label}'
        for key, label, hint in FINANCIAL_TABLE_METRICS
    )
    periods_desc = "\n".join(f'- {key}: {label}' for key, label in FINANCIAL_TABLE_PERIODS)
    payload = [{"fact": item["fact"]} for item in facts]

    prompt = f"""你是台灣私募股權團隊的財務數字整理員。請從下方「{company_name}」已驗證的財務
事實中，挑出能對應到多期財務預測表格的數字，填進對應的期別與指標。

期別（period）只能從：
{periods_desc}

指標（metric）只能從（括號內是補充資料可能出現的會計科目措辭，看到這些字眼就對應到該 metric）：
{metrics_desc}

規則：
1. 只填 evidence 明確支持的期別與指標；不確定、要推算或未提及的一律不要輸出該項。
2. 大多數 evidence 只會對應到「Now」（目前/最近一期實際數字），這是正常情況；
   Now+1、Now+2、Now+3 只有在 evidence 明確標示為未來預測/目標時才填。
3. 數字只能輸出純數字（可含千分位逗號），不要加「元」「新台幣」等單位文字；
   如果原文是萬元、億元等大單位，換算成基本元的數字再輸出（例如「1.2億元」寫成
   "120,000,000"）；毛利率(%) 這類本身就是百分比的欄位，維持含 % 的寫法。
4. 每個有填數字的期別，額外用 period_label 標出這期實際代表的財報期間或截止
   日期，格式跟 evidence 原文一致（例如「114/8/31」或「114年1-8月」），
   找不到明確期間就整個省略 period_label，不要用「Now」這種代稱敷衍。
5. 只輸出 JSON，格式如下，缺的期別或指標整個省略：
{{"now":{{"period_label":"114/8/31","revenue":"14,452,108"}}, "now1":{{}}}}

已驗證財務事實：
{json.dumps(payload, ensure_ascii=False)}
"""
    try:
        raw = await asyncio.to_thread(claude_client.ask, prompt, 120, None, engine)
        data = _json_object(raw)
    except Exception as e:
        log.warning("財務表格統整失敗，整表留空：%s", e)
        return result

    metric_keys = {key for key, _, _ in FINANCIAL_TABLE_METRICS}
    for period_key, _ in FINANCIAL_TABLE_PERIODS:
        period_data = data.get(period_key)
        if not isinstance(period_data, dict):
            continue
        period_label = str(period_data.get("period_label") or "").strip()
        if period_label:
            result[f"fin_period_label_{period_key}"] = period_label
        for metric_key, value in period_data.items():
            if metric_key not in metric_keys or not value:
                continue
            cleaned = str(value).strip()
            if metric_key != "gross_margin_pct":
                cleaned = _strip_currency_unit(cleaned)
            result[f"fin_{period_key}_{metric_key}"] = cleaned
    return result


async def _synthesize_field_group(
    company_name: str,
    keys: tuple[str, ...],
    evidence: dict[str, list[dict[str, str]]],
    engine: str,
) -> dict:
    payload: dict[str, list[dict[str, str]]] = {}
    for key in keys:
        payload[key] = [
            {"id": item["id"], "fact": item["fact"]}
            for item in _deduplicated_facts(evidence[key])
        ]

    prompt = f"""你是台灣私募股權團隊的 Call Memo 編輯。請將「{company_name}」的已驗證事實整理成可直接放進正式備忘錄的欄位文字。

規則：
1. 只能使用下方 evidence，不得加入外部資訊、推測或常識。
2. 合併語意重複的事實；不要逐條照抄，不要暴露逐字稿碎片。
3. 全文使用自然、專業的台灣繁體中文；修正明顯語音辨識用字，但不可改變事實。
4. 短欄位（姓名、日期、地址、金額）只填直接答案。敘述欄位通常 100–350 字，資訊很多時最多 500 字。
5. 使用完整句子與正常標點。禁止「。；」「；。」，禁止同一句換句話說重複出現。
6. 未有 evidence 的欄位輸出空字串。
7. 每個非空欄位列出實際採用的 evidence_ids；ID 必須來自該欄位。
8. 條列優先：只要欄位內容可以拆成 2 個以上各自成立、獨立一句就能講完的重點
   ——不限於「並列個體」，也包括同一段裡的多個產品/技術/服務項目、多項財務
   數字（營收、毛利、毛利率、營業利益、稅後淨利...各自一行）、增資或投資條件
   的各項條款、多個里程碑或客戶——每一項獨立成一行、行首加「• 」，不要合併寫
   成一整段連續文字。像簡報條列一樣，讀者要能一眼看出這裡有幾個重點，不用逐句
   細讀才找得到。
   只有內容本質上就是單一件事、拆開反而破壞語意的敘述（例如一個地址、一句話
   講完的單一結論）才維持一般段落，不要為了條列硬拆。拿不定主意時優先條列。
9. 只輸出 JSON，格式如下（text 裡的換行請直接用 \\n）：
{{"field_key":{{"text":"整理後文字","evidence_ids":["field_key:1"]}}}}

欄位邊界：
- financials 只放公司已發生的營收、獲利、成長率或明確財務狀況；一般營運風險放 risk_tracking。
- factory_capacity 只放公司本身的廠房、實體產能、良率或稼動率；軟體交付人力、開發速度及第三方案例均留空。
- competitors 只整理實際競爭者、替代方案或受訪者明確描述的競爭情境，不要拿公司管理優勢填充。

已驗證 evidence：
{json.dumps(payload, ensure_ascii=False)}
"""
    raw = await asyncio.to_thread(
        claude_client.ask, prompt, 240, None, engine
    )
    data = _json_object(raw)
    return {key: data.get(key, {}) for key in keys}


def _normalize_memo_prose(value: str) -> str:
    text = re.sub(r"[ \t]+", " ", value or "").strip()
    text = re.sub(r"。\s*；", "。", text)
    text = re.sub(r"；\s*。", "。", text)
    text = re.sub(r"；{2,}", "；", text)
    text = re.sub(r"。{2,}", "。", text)
    return text


def fill_template(company: dict, memo: dict, interview_date: str = "") -> bytes:
    """Fill the Call Memo .docx template with memo fields, return bytes."""
    from docx import Document
    from docx.oxml.ns import qn
    from copy import deepcopy
    import io

    template_path = Path(__file__).parent.parent / "data" / "call_memo_template.docx"
    doc = Document(str(template_path))

    # ── Fill header paragraphs (訪談日期 / 評估人) ──────────────────────────
    for para in doc.paragraphs:
        if "訪談日期：" in para.text and interview_date:
            for run in para.runs:
                if "2025/X/X" in run.text or "X/X" in run.text:
                    run.text = run.text.replace("2025/X/X", interview_date).replace("X/X", interview_date)
                    break

    # ── Label → field key mapping（單一來源 FIELDS 衍生，不再手抄 24 條）─────────
    LABEL_MAP = {label: key for key, label, _ in FIELDS}
    LABEL_MAP.update({f"{label}：": key for key, label, _ in FIELDS})  # 範本用「：」結尾
    LABEL_MAP.update({   # 範本文字與 FIELDS 標籤不同的變體
        "公司名稱：": "_company_name",
        "會計師：": "auditor",
        "董監(或主要股東)持股情形：": "board_shareholding",
    })

    company_name = company.get("name", "")

    def _get_value(field_key: str) -> str:
        if field_key == "_company_name":
            return company_name
        return memo.get(field_key, "")

    def _fill_cell(cell, value: str):
        """Keep first paragraph (label), remove rest, add value paragraphs."""
        paragraphs = cell.paragraphs
        if not paragraphs:
            return

        # Remove all paragraphs after the first
        tc = cell._tc
        for p in paragraphs[1:]:
            tc.remove(p._p)

        # First paragraph: keep label runs, remove any non-bold value runs
        first_p = cell.paragraphs[0]
        # Find where label ends (last bold run ending with ：)
        label_end_idx = -1
        for i, run in enumerate(first_p.runs):
            if run.bold or (run.text and run.text.strip().endswith("：")):
                label_end_idx = i

        # Remove runs after label
        for run in first_p.runs[label_end_idx + 1:]:
            first_p._p.remove(run._r)

        if not value:
            return

        # Add value: same paragraph for short values, new paragraphs for multiline
        lines = [l for l in value.split("\n") if l.strip()]
        if not lines:
            return

        # Add first line to label paragraph
        r = first_p.add_run(" " + lines[0])
        r.bold = False

        # Additional lines as new paragraphs
        for line in lines[1:]:
            new_p = cell.add_paragraph()
            new_p.add_run(line)

    # ── 財務狀況儲存格裡的巢狀表格（年度 x Now/Now+1/Now+2/Now+3）─────────────────
    _fin_metric_by_label = {label: key for key, label, _ in FINANCIAL_TABLE_METRICS}
    _fin_period_by_label = {label: key for key, label in FINANCIAL_TABLE_PERIODS}

    def _set_cell_text(target, value: str):
        for p in target.paragraphs[1:]:
            target._tc.remove(p._p)
        first_p = target.paragraphs[0] if target.paragraphs else target.add_paragraph()
        for run in list(first_p.runs):
            first_p._p.remove(run._r)
        if value:
            first_p.add_run(value)

    def _fill_financials_table(cell):
        if not cell.tables:
            return
        fin_table = cell.tables[0]
        if not fin_table.rows:
            return
        period_by_col: dict[int, str] = {}
        header_cells = fin_table.rows[0].cells
        for col_idx, header_cell in enumerate(header_cells):
            period_key = _fin_period_by_label.get(header_cell.text.strip())
            if period_key:
                period_by_col[col_idx] = period_key
        # 表頭預設是 Now/Now+1/Now+2/Now+3；有實際財報期間（例如 114/8/31）時換成
        # 真正的期間文字，讓表格反映補充資料本身的期別，不是永遠顯示通用代稱。
        for col_idx, period_key in period_by_col.items():
            period_label = memo.get(f"fin_period_label_{period_key}", "")
            if period_label:
                _set_cell_text(header_cells[col_idx], period_label)
        for row in fin_table.rows[1:]:
            cells = row.cells
            metric_key = _fin_metric_by_label.get(cells[0].text.strip())
            if not metric_key:
                continue
            for col_idx, period_key in period_by_col.items():
                if col_idx >= len(cells):
                    continue
                value = memo.get(f"fin_{period_key}_{metric_key}", "")
                _set_cell_text(cells[col_idx], value)

    # ── Iterate table cells ───────────────────────────────────────────────────
    if doc.tables:
        table = doc.tables[0]
        for row in table.rows:
            for cell in row.cells:
                cell_text = cell.text.strip()
                matched_key = None
                matched_label = None
                for label, key in LABEL_MAP.items():
                    if cell_text.startswith(label) or cell_text == label.rstrip("：") + "：":
                        matched_key = key
                        matched_label = label
                        break
                if matched_key:
                    _fill_cell(cell, _get_value(matched_key))
                    if matched_key == "financials":
                        _fill_financials_table(cell)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
