"""
TIPO Patent Scraper
Searches tiponet.tipo.gov.tw by company name (AF field) and inventor (IV field).
The site uses a JavaScript Math.random() anti-proxy redirect that is trivially
reproducible with Python — no browser automation needed.
"""

import asyncio
import random
import re
from datetime import date

import httpx
from bs4 import BeautifulSoup

_BASE = "https://tiponet.tipo.gov.tw"
_PATH = "/twpat1/twpatc/twpatkm"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# Matches a result row: patent_no  pub_date  app_no  app_date  title…(stops before link text)
_ROW_RE = re.compile(
    r'\b([IMD]\d{5,10})\b'
    r'\s+(\d{4}/\d{2}/\d{2})'
    r'\s+(\d{7,12})'
    r'\s+(\d{4}/\d{2}/\d{2})'
    r'\s+(.*?)'
    r'(?=\s*(?:專利公報|公告說明書|公開說明書|公開公報))',
    re.DOTALL,
)
_STATUS_RE      = re.compile(r'(核准|撤銷|消滅|未審查/公開|未審查|核駁|結案)')
_CHINESE_RE     = re.compile(r'([一-鿿]{2,5})\s*[（(](?:中華民國|台灣)[）)]')
_APPLICANT_RE   = re.compile(r'申請人\s+([一-鿿\w]{2,30}(?:股份有限公司|有限公司|股份公司|大學|學院|研究院|研究所)?)\s')
_EN_TITLE_RE = re.compile(r'\b[A-Z]{2,}(?:\s+[A-Z]+){2,}\b')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_form(html: str) -> tuple[str, dict]:
    """Return (action_url, base_form_data) from any TIPO page."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        return "", {}
    action = _BASE + form.get("action", _PATH)
    data: dict[str, str] = {}
    for inp in form.find_all("input"):
        n, v, t = inp.get("name", ""), inp.get("value", ""), inp.get("type", "text")
        if not n or t == "submit":
            continue
        # Skip checkbox filters — they inject extra AND conditions into the query
        if any(n.startswith(p) for p in ("_0_54_", "_0_55_", "_0_56_", "_0_57_", "_0_58_")):
            continue
        data[n] = v
    return action, data


async def _fresh_form(client: httpx.AsyncClient) -> tuple[str, dict]:
    """Get a clean search form by re-following the JS redirect."""
    r = await client.get(f"{_BASE}{_PATH}?@@{random.random()}")
    return _get_form(r.text)


def _parse_results(html: str) -> list[dict]:
    """Extract patent list from TIPO results page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    patents: list[dict] = []
    seen: set[str] = set()

    for m in _ROW_RE.finditer(text):
        patent_no, pub_date, app_no, app_date, title_raw = m.groups()
        if patent_no in seen:
            continue
        seen.add(patent_no)

        # Status appears just after the link-label delimiter
        status_m = _STATUS_RE.search(text[m.end(): m.end() + 60])
        status = status_m.group(1) if status_m else "—"

        # Keep only the Chinese portion — strip everything from the first Latin letter
        # that follows a CJK character (the English title duplicate)
        title = title_raw.strip()
        en_start = re.search(r'(?<=[一-鿿])\s+[A-Za-z]', title)
        if en_start:
            title = title[:en_start.start()].strip()

        patents.append({
            "patent_no":  patent_no,
            "pub_date":   pub_date.replace("/", "-"),
            "app_no":     app_no,
            "app_date":   app_date.replace("/", "-"),
            "title":      title,
            "status":     status,
            "applicant":  "",
            "inventors":  [],
            "brief":      "",
            "fetched_at": date.today().isoformat(),
        })

    return patents


def _parse_detail(html: str) -> dict:
    """Extract applicant, inventors and abstract from a patent detail page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Applicant (法人): first Chinese entity name in the 申請人 section
    applicant = ""
    app_idx = text.find("申請人")
    if app_idx >= 0:
        area = text[app_idx: app_idx + 300]
        m = _APPLICANT_RE.search(area)
        if m:
            applicant = m.group(1)

    # Inventors (自然人): Chinese names followed by (中華民國) or (台灣)
    inventors: list[str] = []
    for kw in ("發明人", "創作人"):
        idx = text.find(kw)
        if idx >= 0:
            area = text[idx: idx + 500]
            stop = re.search(r'代理人|審查委員|摘要|申請人', area[4:])
            if stop:
                area = area[:4 + stop.start()]
            inventors = list(dict.fromkeys(_CHINESE_RE.findall(area)))  # preserve order, dedup
            break

    # Abstract
    brief = ""
    abs_idx = text.find("摘要")
    if abs_idx >= 0:
        snippet = text[abs_idx + 2: abs_idx + 600]
        stop = re.search(r'申請專利範圍|說明書|圖式|發明說明', snippet)
        if stop:
            snippet = snippet[:stop.start()]
        brief = snippet.strip()[:300]

    return {"applicant": applicant, "inventors": inventors, "brief": brief}


# ── Main workflow ─────────────────────────────────────────────────────────────

async def scrape_company_patents(company: dict, on_event) -> list[dict]:
    """
    Full TIPO patent workflow:
    1. Search by applicant name (AF)
    2. Fetch detail pages for first 15 patents → extract inventors + abstract
    3. Reverse-search by each unique inventor (IV)
    4. Deduplicate and return sorted list (newest app_date first)
    """
    company_name = company.get("name", "")

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:

        # ① Init
        await on_event({"type": "progress", "message": "連接 TIPO 系統…"})
        action, base = await _fresh_form(client)

        # ② Company-name search — try multiple name variants to handle short vs full legal names
        suffixes = ["", "股份有限公司", "有限公司", "科技股份有限公司", "生技股份有限公司"]
        base_name = re.sub(r'(股份有限公司|有限公司|股份公司)$', '', company_name).strip()
        candidates = list(dict.fromkeys(
            [company_name] + [base_name + s for s in suffixes if s and base_name + s != company_name]
        ))

        company_patents: list[dict] = []
        matched_name = company_name
        for cand in candidates:
            await on_event({"type": "progress", "message": f"搜尋申請人：{cand}"})
            r = await client.post(action, data={**base, "_5_5_T": f"AF=({cand})", "BUTTON": "檢索"})
            company_patents = _parse_results(r.text)
            action, base = _get_form(r.text)
            if company_patents:
                matched_name = cand
                break
            await asyncio.sleep(0.3)

        count_m = re.search(r'全部結果[^>]*?>\s*\(?(\d+)\)?', r.text) or re.search(r'\((\d+)筆\)', r.text)
        total_str = count_m.group(1) if count_m else str(len(company_patents))
        await on_event({"type": "progress", "message": f"找到 {total_str} 筆（{matched_name}），分析發明人…"})

        all_patents: dict[str, dict] = {p["patent_no"]: p for p in company_patents}
        inventors_found: set[str] = set()

        # ③ Fetch detail pages for first 15 results
        for i, pat in enumerate(company_patents[:15]):
            await asyncio.sleep(0.4)
            try:
                r2 = await client.post(action, data={**base, "_5_5_T": f"PN=({pat['patent_no']})", "BUTTON": "檢索"})
                soup2 = BeautifulSoup(r2.text, "html.parser")
                detail_a = soup2.find("a", string=lambda t: t and t.strip() == pat["patent_no"])
                if detail_a and detail_a.get("href"):
                    r3 = await client.get(_BASE + detail_a["href"])
                    det = _parse_detail(r3.text)
                    pat["applicant"] = det["applicant"]
                    pat["inventors"] = det["inventors"]
                    pat["brief"]     = det["brief"]
                    inventors_found.update(det["inventors"])
                action, base = _get_form(r2.text)
            except Exception:
                pass
            await on_event({"type": "progress", "message": f"讀取發明人 {i+1}/15：{pat['patent_no']}"})

        # ④ Inventor reverse-search
        await asyncio.sleep(0.5)
        action, base = await _fresh_form(client)

        for inventor in list(inventors_found)[:8]:
            await on_event({"type": "progress", "message": f"反查發明人：{inventor}"})
            await asyncio.sleep(0.5)
            try:
                r4 = await client.post(action, data={**base, "_5_5_T": f"IV=({inventor})", "BUTTON": "檢索"})
                for p in _parse_results(r4.text):
                    if p["patent_no"] not in all_patents:
                        p["inventors"] = [inventor]
                        all_patents[p["patent_no"]] = p
                action, base = _get_form(r4.text)
            except Exception:
                pass

        result = sorted(all_patents.values(), key=lambda x: x.get("app_date", ""), reverse=True)
        await on_event({"type": "progress", "message": f"完成：共收錄 {len(result)} 筆專利"})
        return result
