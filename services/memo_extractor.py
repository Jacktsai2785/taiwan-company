"""
Extract Call Memo fields from a meeting transcript using Claude CLI.
Returns a dict matching the 20-field template schema.
"""
import asyncio
import json
import re
from pathlib import Path

from services import claude_client

# Ordered field definitions: key -> (label, short_description)
FIELDS: list[tuple[str, str, str]] = [
    ("deal_source",        "案件來源",                   "例：自行開發、某人介紹。若未提及請填「自行開發」"),
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
    ("financials",         "財務狀況",                   "近期營收、淨利、年增率等財務數據"),
    ("management_team",    "經營團隊背景",                "創辦人/CEO、CTO、CFO 的背景與經歷"),
    ("board_shareholding", "董監或主要股東持股情形",       "主要股東名稱與持股比例"),
    ("recent_development", "公司發展近況",                "近期重大里程碑、產品進展、合作案"),
    ("major_customers",    "主要銷貨客戶",                "前幾大客戶名稱與佔比"),
    ("major_suppliers",    "主要進貨廠商",                "主要原物料或零件供應商"),
    ("factory_capacity",   "廠房及產能使用情形",          "廠房地點、產能規模、目前使用率"),
    ("competitors",        "國內外主要競爭對手",          "直接競爭者名稱及差異化分析"),
    ("industry_trends",    "產業發展趨勢",                "產業現況、市場規模、未來展望"),
    ("risk_tracking",      "風險評估及追蹤事項",          "主要風險點與需持續追蹤的議題"),
    ("conclusion",         "評估結論與建議",              "訪談整體評估與後續建議行動"),
]

FIELD_KEYS = [f[0] for f in FIELDS]


async def extract_from_transcript(company_name: str, transcript: str, engine: str = "claude") -> dict:
    """Use Claude to extract all 20 Call Memo fields from a transcript."""
    fields_desc = "\n".join(
        f'  "{key}": "{label}（{desc}）"'
        for key, label, desc in FIELDS
    )

    prompt = f"""你是一位專業的投資分析師助理。以下是與「{company_name}」的訪談逐字稿。

請從逐字稿中提取以下欄位的資訊，以 JSON 格式回傳。
- 若某欄位在逐字稿中有提及，請整理成清楚的中文句子或條列。
- 若未提及，請回傳空字串 ""。
- 回傳純 JSON，不要加 markdown code block 或其他說明。

需提取的欄位：
{{
{fields_desc}
}}

逐字稿內容：
---
{transcript[:12000]}
---

請直接回傳 JSON 物件。"""

    raw = await asyncio.to_thread(claude_client.ask, prompt, 180, None, engine)

    # Strip markdown fences if present
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"\n?```$", "", raw.strip(), flags=re.MULTILINE)

    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group()) if m else {}
    except Exception:
        data = {}

    # Ensure all keys present, unknown keys filtered out
    return {k: str(data.get(k, "")) for k in FIELD_KEYS}


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

    # ── Label → field key mapping ─────────────────────────────────────────────
    LABEL_MAP = {label: key for key, label, _ in FIELDS}
    # Also add variants that appear in the template
    LABEL_MAP.update({
        "案件來源：": "deal_source",
        "公司名稱：": "_company_name",
        "受訪人：": "interviewees",
        "實收資本額：": "paid_in_capital",
        "地址：": "address",
        "設立日期：": "founding_date",
        "承銷商：": "underwriter",
        "會計師：": "auditor",
        "董事長：": "chairman",
        "總經理：": "general_manager",
        "員工人數：": "headcount",
        "公開發行及上市櫃時程/募資規劃：": "ipo_timeline",
        "增資計畫或投資條件：": "investment_terms",
        "主要業務、產品營收比重：": "business_revenue",
        "財務狀況：": "financials",
        "經營團隊背景：": "management_team",
        "董監(或主要股東)持股情形：": "board_shareholding",
        "公司發展近況：": "recent_development",
        "主要銷貨客戶：": "major_customers",
        "主要進貨廠商：": "major_suppliers",
        "廠房及產能使用情形：": "factory_capacity",
        "國內外主要競爭對手：": "competitors",
        "產業發展趨勢：": "industry_trends",
        "風險評估及追蹤事項：": "risk_tracking",
        "評估結論與建議：": "conclusion",
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

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
