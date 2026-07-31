"""
TIPO Patent Scraper
Searches tiponet.tipo.gov.tw by company name (AF field) and inventor (IV field).
The site uses a JavaScript Math.random() anti-proxy redirect that is trivially
reproducible with Python — no browser automation needed.
"""

import asyncio
import logging
import random
import re
from datetime import date

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

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
_EN_TITLE_RE    = re.compile(r'\b[A-Z]{2,}(?:\s+[A-Z]+){2,}\b')
# IPC main class: <Section letter><2 digits><Class letter>, e.g. "G08B", "G01V".
# Used as a "business domain" signal for inventor-reverse-search filtering.
_IPC_MAIN_RE    = re.compile(r'\b([A-H]\d{2}[A-Z])\b')


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
    action, base = _get_form(r.text)
    if not action:
        raise RuntimeError("TIPO 系統無法連線或拒絕存取（可能封鎖雲端 IP），請稍後再試")
    return action, base


def _max_page_size_url(html: str) -> str | None:
    """Find the 'show 100 per page' GET link in TIPO results header.

    TIPO's per-page selector is a <select> whose <option value=...> is a full
    URL; switching page size means following that URL, not POSTing a param.
    Without this, AF/IV searches silently truncate at the 10-row default.
    """
    soup = BeautifulSoup(html, "html.parser")
    for opt in soup.find_all("option"):
        if opt.get_text(strip=True) == "100":
            val = opt.get("value", "").strip()
            if val:
                return _BASE + val
    return None


async def _search(client: httpx.AsyncClient, action: str, base: dict, query: str) -> httpx.Response:
    """POST a TIPO search and auto-expand the result page to 100 rows."""
    r = await client.post(action, data={**base, "_5_5_T": query, "BUTTON": "檢索"})
    big_url = _max_page_size_url(r.text)
    if big_url:
        try:
            r = await client.get(big_url)
        except Exception as exc:
            log.warning("page-size expand failed for %s: %s", query, exc)
    return r


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

    # IPC main classes — domain signal (e.g. "G08B", "G01V" for earthquake
    # sensing; "B23Q", "F15B" for CNC machining). Used to gate inventor-
    # reverse-search hits to ones whose business overlaps the target company.
    ipc: set[str] = set()
    for kw in ("當前IPC", "公報IPC"):
        idx = text.find(kw)
        if idx >= 0:
            area = text[idx + len(kw): idx + len(kw) + 800]
            stop = re.search(r'LOC|申請人|當前專利權人|專利名稱', area)
            if stop:
                area = area[:stop.start()]
            ipc.update(_IPC_MAIN_RE.findall(area))
            if ipc:
                break

    return {"applicant": applicant, "inventors": inventors, "brief": brief, "ipc": sorted(ipc)}


def _is_exact_applicant(applicant: str, legal_name: str) -> bool:
    """Require the TIPO applicant to be the selected legal entity.

    Applicant searches are substring searches, so 東佑達自動化科技 also returns
    東佑達奈米系統.  Normalising whitespace is safe, but fuzzy/prefix matching is
    deliberately forbidden here.
    """
    normalize = lambda value: re.sub(r"[\s　]", "", value or "")
    return bool(applicant and legal_name and normalize(applicant) == normalize(legal_name))


# ── Main workflow ─────────────────────────────────────────────────────────────

async def scrape_company_patents(company: dict, on_event) -> list[dict]:
    """
    Full TIPO patent workflow:
    1. Search by applicant name (AF)
    2. Fetch every detail page and require an exact legal-entity applicant match
    3. Deduplicate and return sorted list (newest app_date first)
    """
    company_name = company.get("name", "")
    identity_since = (company.get("patent_identity_since") or "").strip()

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=15) as client:

        # ① Init
        await on_event({"type": "progress", "message": "連接 TIPO 系統…"})
        action, base = await _fresh_form(client)

        # ② Company-name search — try multiple name variants to handle short vs full legal names
        suffixes = ["", "股份有限公司", "有限公司", "科技股份有限公司", "生技股份有限公司"]
        base_name = re.sub(r'(股份有限公司|有限公司|股份公司)$', '', company_name).strip()
        target_legal_name = (
            company_name
            if company_name.endswith(("股份有限公司", "有限公司", "股份公司"))
            else base_name + ("股份有限公司" if company.get("is_corp") else "")
        )
        candidates = list(dict.fromkeys(
            [target_legal_name, company_name]
            + [base_name + s for s in suffixes if s and base_name + s != company_name]
        ))

        company_patents: list[dict] = []
        matched_name = company_name
        for cand in candidates:
            await on_event({"type": "progress", "message": f"搜尋申請人：{cand}"})
            r = await _search(client, action, base, f"AF=({cand})")
            company_patents = _parse_results(r.text)
            new_action, new_base = _get_form(r.text)
            if new_action:
                action, base = new_action, new_base
            if company_patents:
                matched_name = cand
                break
            await asyncio.sleep(0.3)

        # NB: TIPO's "全部結果 (N)" double-counts a single invention filed as
        # both 發明 and 新型 (台灣常見的「一案兩請」). Report the deduped count
        # to match what actually ends up in the list.
        await on_event({"type": "progress",
                        "message": f"找到 {len(company_patents)} 筆（{matched_name}），分析發明人…"})

        # ③ Build detail-link map from the company search results page directly.
        #    The results page already contains <a href=...>I923838</a> style links —
        #    no need for a per-patent PN re-search (which breaks due to TIPO session
        #    pagination state and returns the wrong page).
        detail_links: dict[str, str] = {}
        soup_r = BeautifulSoup(r.text, "html.parser")
        for a in soup_r.find_all("a", href=True):
            txt = a.get_text(strip=True)
            if txt and txt[0] in "IMD" and txt[1:].isdigit():
                detail_links[txt] = _BASE + a["href"]

        verified_patents: list[dict] = []
        for i, pat in enumerate(company_patents):
            await asyncio.sleep(0.15)
            pno = pat["patent_no"]
            href = detail_links.get(pno)
            if not href:
                log.warning("detail link not found for %s", pno)
                await on_event({"type": "progress", "message": f"驗證申請人 {i+1}/{len(company_patents)}：{pno}"})
                continue
            try:
                r3 = await client.get(href)
                det = _parse_detail(r3.text)
                identity_date_match = (
                    not identity_since
                    or (pat.get("app_date") or "") >= identity_since
                )
                if (
                    identity_date_match
                    and _is_exact_applicant(det["applicant"], target_legal_name)
                ):
                    pat["applicant"] = det["applicant"]
                    pat["inventors"] = det["inventors"]
                    pat["brief"]     = det["brief"]
                    pat["ipc"]       = det["ipc"]
                    verified_patents.append(pat)
            except Exception as exc:
                log.warning("detail fetch failed for %s: %s", pno, exc)
            await on_event({"type": "progress", "message": f"驗證申請人 {i+1}/{len(company_patents)}：{pno}"})

        result = sorted(verified_patents, key=lambda x: x.get("app_date", ""), reverse=True)
        await on_event({"type": "progress", "message": f"完成：共收錄 {len(result)} 筆專利"})
        return result
