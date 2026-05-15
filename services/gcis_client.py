"""
Fetch company data from ronnywang g0v company search API.
https://company.g0v.ronny.tw/api/search?q={company_name}

Single request returns: tax_id, capital, representative, address,
par_value, total_shares, and full director list.
Falls back to GCIS App1 API (by tax_id) when ronnywang returns no match.
GCIS App1 also provides setup_date, last_change_date, and register_org.
Listing status (上市/上櫃/興櫃/創新板/非公發) is resolved from TWSE/TPEX open APIs.
創櫃板: TPEX 尚未提供公開 JSON API，暫不支援自動辨識。
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx

RONNY_SEARCH = "https://company.g0v.ronny.tw/api/search"
RONNY_FUND = "https://company.g0v.ronny.tw/api/fund"
RONNY_NAME = "https://company.g0v.ronny.tw/api/name"
GCIS_APP1 = (
    "https://data.gcis.nat.gov.tw/od/data/api/"
    "5F64D864-61CB-4D0D-8AD9-492047CC1EA6"
)
TIMEOUT = 20.0

# ── Listing status cache ───────────────────────────────────────────────────────
# _by_taxid / _by_name: 上市/上櫃/興櫃, matched by tax_id or full name
# _by_abbrev: 創新板(GISA), matched by abbreviated name (no 股份有限公司 suffix)
_by_taxid:  dict[str, str] = {}
_by_name:   dict[str, str] = {}
_by_abbrev: dict[str, str] = {}
_cache_until: datetime | None = None
_cache_lock = asyncio.Lock()
_CACHE_TTL = timedelta(hours=24)

_LISTING_SOURCES = [
    ("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", "上市"),
    ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", "上櫃"),
    ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R", "興櫃"),
]
# 創新板(GISA): returns abbreviated names with no tax_id; needs Accept: application/json
_GISA_URL = "https://www.tpex.org.tw/openapi/v1/tpex_gisa_company"

_NAME_SUFFIXES = ("股份有限公司", "有限公司")


async def _load_listing_source(client: httpx.AsyncClient, url: str, status: str) -> None:
    try:
        resp = await client.get(url, timeout=15.0)
        resp.raise_for_status()
        rows = resp.json()
        for row in rows:
            taxid = (
                row.get("營利事業統一編號") or   # TWSE 上市
                row.get("UnifiedBusinessNo.") or  # TPEX 上櫃/興櫃
                row.get("統一編號") or ""
            ).strip()
            cname = (
                row.get("公司名稱") or row.get("CompanyName") or ""
            ).strip()
            if taxid:
                _by_taxid[taxid] = status
            if cname:
                _by_name[cname] = status
    except Exception:
        pass


async def _load_gisa(client: httpx.AsyncClient) -> None:
    """Load 創新板 (GISA) companies. API returns abbreviated names, no tax_id."""
    try:
        resp = await client.get(
            _GISA_URL,
            timeout=15.0,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        for row in resp.json():
            abbrev = (row.get("CompanyName") or "").strip()
            if abbrev:
                _by_abbrev[abbrev] = "創新板"
    except Exception:
        pass


async def _ensure_listing_cache(client: httpx.AsyncClient) -> None:
    global _cache_until
    if _cache_until and datetime.now() < _cache_until:
        return
    async with _cache_lock:
        if _cache_until and datetime.now() < _cache_until:
            return
        _by_taxid.clear()
        _by_name.clear()
        _by_abbrev.clear()
        for url, status in _LISTING_SOURCES:
            await _load_listing_source(client, url, status)
        await _load_gisa(client)
        _cache_until = datetime.now() + _CACHE_TTL


def _resolve_listing_status(tax_id: str, name: str) -> str:
    # 1. Tax ID match (上市/上櫃/興櫃)
    if tax_id and tax_id in _by_taxid:
        return _by_taxid[tax_id]
    # 2. Full name match (上市/上櫃/興櫃)
    if name and name in _by_name:
        return _by_name[name]
    # 3. Abbreviated name match for 創新板 (strip company-type suffix)
    abbrev = name
    for sfx in _NAME_SUFFIXES:
        if abbrev.endswith(sfx):
            abbrev = abbrev[: -len(sfx)]
            break
    if abbrev and abbrev != name and abbrev in _by_abbrev:
        return _by_abbrev[abbrev]
    return "非公發"


def pick_largest_legal_director(directors: list[dict]) -> dict | None:
    """Pick the director with the highest ratio whose representative_of (法人) is set.

    Falls back to None when no director represents a legal entity. The strict rule
    (largest shareholder must already be a 法人) is intentionally relaxed here per
    the user's directive: if the top shareholder is an 自然人, fall through to the
    next largest with a representative_of so we can still surface a parent.
    """
    candidates = [
        d for d in (directors or [])
        if (d.get("representative_of") or "").strip()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.get("ratio") or 0)


async def fetch_parent_entity_data(name: str, tax_id: str = "") -> dict[str, Any]:
    """Fetch parent legal entity data by name (and tax_id if available).

    Returns a flat dict with name/tax_id/capital/listing_status/representative.
    Empty dict when API yields nothing usable.
    """
    if not name and not tax_id:
        return {}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        await _ensure_listing_cache(client)

        resolved_name = name
        if tax_id and not resolved_name:
            resolved_name = await _ronny_name_by_tax_id(client, tax_id)

        ronny: dict[str, Any] = {}
        if resolved_name:
            ronny = await _fetch_ronny(client, resolved_name) or {}

        out: dict[str, Any] = {
            "name": ronny.get("matched_name") or resolved_name or name,
            "tax_id": ronny.get("tax_id") or tax_id,
            "capital": ronny.get("capital", 0),
            "representative": ronny.get("representative", ""),
            "address": ronny.get("address", ""),
        }

        # GCIS supplement when we have a tax_id (authorized_capital, setup_date)
        if out["tax_id"]:
            gcis = await _fetch_gcis_by_tax_id(client, out["tax_id"])
            if not out["representative"] and gcis.get("representative"):
                out["representative"] = gcis["representative"]
            if not out["capital"] and gcis.get("capital"):
                out["capital"] = gcis["capital"]
            if gcis.get("authorized_capital"):
                out["authorized_capital"] = gcis["authorized_capital"]

        out["listing_status"] = _resolve_listing_status(out["tax_id"], out["name"])
        return out


async def fetch_subsidiaries_of_legal_entity(name: str, tax_id: str = "") -> list[dict]:
    """Reverse lookup: companies in which the given legal entity holds a director seat.

    Uses Ronny's `/api/fund?q=` endpoint, which returns full company records (with
    董監事名單). For each result, we identify the director(s) representing the parent
    and compute their share ratio against that company's 已發行股份總數(股).

    Returns a list of {name, tax_id, via_director, shares, ratio}, sorted by ratio desc.
    """
    query = (tax_id or name or "").strip()
    if not query:
        return []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.get(RONNY_FUND, params={"q": query}, timeout=20.0)
            resp.raise_for_status()
            hits = resp.json().get("data", []) or []
        except Exception:
            return []

    out: list[dict] = []
    for h in hits:
        sub_name = h.get("公司名稱", "")
        sub_tax_id = h.get("統一編號", "")
        sub_total_shares = _parse_int(h.get("已發行股份總數(股)", "0"))

        # Among directors of this subsidiary, find the one(s) representing the parent.
        # Pick the largest such holding (some parents have multiple representatives).
        best_director_name = ""
        best_shares = 0
        for d in (h.get("董監事名單") or []):
            rep = d.get("所代表法人")
            if not isinstance(rep, list) or len(rep) < 2:
                continue
            rep_tax_id = str(rep[0]) if rep[0] else ""
            rep_name = str(rep[1]) if rep[1] else ""
            tax_match = bool(tax_id) and rep_tax_id == tax_id
            name_match = bool(name) and rep_name == name
            if not (tax_match or name_match):
                continue
            shares = _parse_int(d.get("出資額", "0"))
            if shares > best_shares:
                best_shares = shares
                best_director_name = d.get("姓名", "")

        if best_shares == 0 and not best_director_name:
            # The fund endpoint returned this row but the parent isn't actually a representative
            # in current 董監事名單 (could be stale data). Skip.
            continue

        out.append({
            "name": sub_name,
            "tax_id": sub_tax_id,
            "via_director": best_director_name,
            "shares": best_shares,
            "ratio": round(best_shares / sub_total_shares, 6) if sub_total_shares > 0 else 0.0,
        })

    out.sort(key=lambda s: s["ratio"], reverse=True)
    return out


async def fetch_companies_of_person(person_name: str) -> list[dict]:
    """Reverse lookup: companies in which the given natural person serves as a director/supervisor.

    Uses Ronny `/api/name?q=`. Caveat: results include ALL persons with this exact name,
    so the caller / UI must warn users that homonym resolution is not possible.

    Returns list of {name, tax_id, title, role_kind, ratio, shares, represents_legal_entity}.
    """
    name = (person_name or "").strip()
    if not name:
        return []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.get(RONNY_NAME, params={"q": name}, timeout=20.0)
            resp.raise_for_status()
            hits = resp.json().get("data", []) or []
        except Exception:
            return []

    out: list[dict] = []
    for h in hits:
        co_name = h.get("公司名稱", "")
        co_tax_id = h.get("統一編號", "")
        co_total_shares = _parse_int(h.get("已發行股份總數(股)", "0"))

        best_title = ""
        best_shares = 0
        represents_entity = ""
        for d in (h.get("董監事名單") or []):
            if d.get("姓名") != name:
                continue
            shares = _parse_int(d.get("出資額", "0"))
            if shares >= best_shares:
                best_shares = shares
                best_title = d.get("職稱", "")
                rep = d.get("所代表法人")
                if isinstance(rep, list) and len(rep) > 1:
                    represents_entity = str(rep[1])

        # If the person isn't actually in current 董監事名單 (stale), fall back to representative role
        if not best_title:
            if h.get("代表人姓名") == name:
                best_title = "公司代表人"

        out.append({
            "name": co_name,
            "tax_id": co_tax_id,
            "title": best_title,
            "shares": best_shares,
            "ratio": round(best_shares / co_total_shares, 6) if co_total_shares > 0 else 0.0,
            "represents_legal_entity": represents_entity,
        })

    out.sort(key=lambda x: (x["ratio"], x["shares"]), reverse=True)
    return out


async def _fetch_ronny_show(client: httpx.AsyncClient, tax_id: str) -> dict | None:
    """/api/show/{tax_id} — returns full company record including 每股金額/已發行股份總數."""
    try:
        resp = await client.get(
            f"https://company.g0v.ronny.tw/api/show/{tax_id}", timeout=10.0
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data")
        if not data or not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


async def fetch_company_name_by_tax_id(tax_id: str) -> str:
    """Return the official company name for a given tax ID, or empty string if not found."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        # Primary: Ronny /api/show/{tax_id}
        data = await _fetch_ronny_show(client, tax_id)
        if data:
            name = data.get("公司名稱", "")
            if name:
                return name
        # Fallback: GCIS App1
        try:
            resp = await client.get(
                GCIS_APP1,
                params={
                    "$format": "json",
                    "$filter": f"Business_Accounting_NO eq '{tax_id}'",
                    "$skip": "0",
                    "$top": "1",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            if rows:
                return rows[0].get("Company_Name", "") or rows[0].get("公司名稱", "")
        except Exception:
            pass
    return ""


_ACTIVE_STATUSES = {"核准設立", "登記"}
_DISSOLVED_STATUSES = {"解散", "廢止", "撤銷", "命令解散"}
_DISSOLVED_KEYWORDS = ("解散", "撤銷", "廢止", "命令解散", "歇業")


async def search_company_matches(name: str) -> list[dict]:
    """Return candidate matches for name disambiguation.

    Runs two parallel Ronny queries (bare name + "name + 股份有限公司") so that
    both 有限公司 and 股份有限公司 variants are captured. Deduplicates by tax_id,
    skips 行號/商號 (no 公司名稱), and filters out dissolved companies.
    Sorts: 股份有限公司 first, then 有限公司; active before unknown status.
    """
    queries = [name]
    if not name.endswith("股份有限公司"):
        queries.append(name + "股份有限公司")

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        async def _fetch_all_pages(q: str, max_pages: int = 20) -> list[dict]:
            """Fetch all pages for a query, up to max_pages."""
            try:
                resp = await client.get(RONNY_SEARCH, params={"q": q, "page": 1}, timeout=10.0)
                resp.raise_for_status()
                data = resp.json()
                results = data.get("data", [])
                found = data.get("found", 0) or 0
                per_page = 10  # Ronny API returns 10 results per page
                total_pages = min(max_pages, -(-found // per_page))  # ceil division
                if total_pages > 1:
                    extra = await asyncio.gather(*[
                        _fetch_page(q, p) for p in range(2, total_pages + 1)
                    ])
                    for page_data in extra:
                        results.extend(page_data)
                return results
            except Exception:
                return []

        async def _fetch_page(q: str, page: int) -> list[dict]:
            try:
                resp = await client.get(RONNY_SEARCH, params={"q": q, "page": page}, timeout=10.0)
                resp.raise_for_status()
                return resp.json().get("data", [])
            except Exception:
                return []

        raw_results = await asyncio.gather(*[_fetch_all_pages(q) for q in queries])

    # Merge, skip 行號/商號 (no 公司名稱), deduplicate by tax_id
    seen_tax: set[str] = set()
    all_hits: list[dict] = []
    for hits in raw_results:
        for h in hits:
            raw_name = h.get("公司名稱")
            if not raw_name:
                continue
            # Ronny occasionally returns 公司名稱 as a list (historical names); normalise to str
            if isinstance(raw_name, list):
                raw_name = raw_name[0] if raw_name else ""
                if not raw_name:
                    continue
                h = dict(h)
                h["公司名稱"] = raw_name
            tid = h.get("統一編號", "")
            if tid and tid in seen_tax:
                continue
            seen_tax.add(tid)
            all_hits.append(h)

    # Step 1: Filter Ronny-known dissolved companies; keep active and unknown-status
    status_key = "公司狀況"
    candidates = [h for h in all_hits if h.get(status_key) in _ACTIVE_STATUSES or h.get(status_key) is None]

    # Step 2: For unknown-status companies, verify against GCIS App1
    unknown = [h for h in candidates if not h.get(status_key)]
    if unknown:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as gc:
            sem = asyncio.Semaphore(5)

            async def _gcis_status(tax_id: str) -> str:
                async with sem:
                    try:
                        resp = await gc.get(
                            GCIS_APP1,
                            params={
                                "$format": "json",
                                "$filter": f"Business_Accounting_NO eq '{tax_id}'",
                                "$skip": "0", "$top": "1",
                            },
                        )
                        resp.raise_for_status()
                        rows = resp.json()
                        return rows[0].get("Company_Status_Desc", "") if rows else ""
                    except Exception:
                        return ""

            tax_ids = [h.get("統一編號", "") for h in unknown]
            gcis_statuses = await asyncio.gather(*[_gcis_status(tid) for tid in tax_ids])

        gcis_map = dict(zip(tax_ids, gcis_statuses))
        for h in unknown:
            gcis_st = gcis_map.get(h.get("統一編號", ""), "")
            if gcis_st:
                h["_gcis_status"] = gcis_st

    # Step 3: Remove GCIS-confirmed dissolved companies
    verified = []
    for h in candidates:
        gcis_st = h.get("_gcis_status", "")
        if gcis_st and any(kw in gcis_st for kw in _DISSOLVED_KEYWORDS):
            continue
        verified.append(h)

    # Step 4: Sort — 股份有限公司 first; known-active before unknown; shorter names first
    # (shorter name = query covers a larger fraction = closer match)
    def _sort_key(h: dict) -> tuple:
        full = h.get("公司名稱", "")
        is_corp = full.endswith("股份有限公司")
        ronny_active = h.get(status_key) in _ACTIVE_STATUSES
        gcis_active = h.get("_gcis_status", "") not in ("", None) and not any(
            kw in h.get("_gcis_status", "") for kw in _DISSOLVED_KEYWORDS
        )
        short = full
        for sfx in _NAME_SUFFIXES:
            if short.endswith(sfx):
                short = short[: -len(sfx)]
                break
        return (not is_corp, not (ronny_active or gcis_active), len(short))

    verified.sort(key=_sort_key)

    result = []
    for h in verified[:50]:
        full_name = h.get("公司名稱", "")
        short_name = full_name
        for sfx in _NAME_SUFFIXES:
            if short_name.endswith(sfx):
                short_name = short_name[:-len(sfx)]
                break
        ronny_st = h.get(status_key) or ""
        gcis_st  = h.get("_gcis_status", "")
        status = ronny_st or gcis_st
        is_corp = full_name.endswith("股份有限公司")
        result.append({
            "full_name": full_name,
            "short_name": short_name,
            "tax_id": h.get("統一編號", ""),
            "status": status,
            "is_corp": is_corp,
        })
    return result


async def fetch_company_data(name: str) -> dict[str, Any]:
    """
    Returns enrichment dict with all available fields.
    Missing fields default to empty string / 0 / [].
    """
    result: dict[str, Any] = {
        "tax_id": "",
        "representative": "",
        "capital": 0,
        "authorized_capital": 0,
        "address": "",
        "listing_status": "非公發",
        "par_value": 0,
        "total_shares": 0,
        "directors": [],
        "setup_date": "",
        "last_change_date": "",
        "register_org": "",
    }

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        await _ensure_listing_cache(client)

        ronny = await _fetch_ronny(client, name)
        if ronny:
            result.update(ronny)

        # If we got a tax_id from the name search, use /api/show/{tax_id} to fetch
        # the full Ronny record which includes 每股金額(元) and 已發行股份總數(股).
        # These fields are often null in /api/search but present in /api/show.
        show_tax_id = result.get("tax_id", "")
        if show_tax_id:
            show_data = await _fetch_ronny_show(client, show_tax_id)
            if show_data:
                par_raw = (show_data.get("每股金額(元)") or "").strip()
                if par_raw == "無票面金額":
                    result["no_par_value"] = True   # 明確的無票面金額股，非資料缺失
                else:
                    par = _parse_int(par_raw or "0")
                    if par and not result.get("par_value"):
                        result["par_value"] = par
                total = _parse_int(show_data.get("已發行股份總數(股)") or "0")
                if total and not result.get("total_shares"):
                    result["total_shares"] = total
                    # Recalculate ratios with newly obtained total_shares
                    for d in result.get("directors", []):
                        d["ratio"] = round((d.get("shares") or 0) / total, 6)

        # Cross-reference with GCIS App1. GCIS is authoritative: any non-empty GCIS value
        # overrides Ronny (Ronny is used for speed; GCIS is the official source).
        tax_id = result.get("tax_id", "")
        if tax_id:
            gcis = await _fetch_gcis_by_tax_id(client, tax_id)
            for k in ("authorized_capital", "capital", "setup_date",
                      "last_change_date", "register_org", "representative", "address"):
                v = gcis.get(k)
                if v:  # non-empty and non-zero overrides Ronny
                    result[k] = v

            # Derive total_shares from GCIS Paid_In_Capital / Ronny par_value when missing
            # Skip for 無票面金額 companies since par_value is not applicable.
            if not result.get("total_shares") and result.get("par_value") and gcis.get("capital") and not result.get("no_par_value"):
                derived = gcis["capital"] // result["par_value"]
                if derived > 0:
                    result["total_shares"] = derived
                    for d in result.get("directors", []):
                        d["ratio"] = round((d.get("shares") or 0) / derived, 6)

        result["listing_status"] = _resolve_listing_status(tax_id, name)

        # Persist is_corp so the frontend can show the 🔍 fetch button correctly
        # even when the stored company name is abbreviated.
        matched = result.get("matched_name", "")
        result["is_corp"] = matched.endswith("股份有限公司")

        # Recalculate director ratios when total_shares is unavailable.
        # For 有限公司: 出資額 = NTD → divide by 實收資本額 (capital).
        # For 股份有限公司: 出資額 = share count → cannot divide by NTD; leave ratio as 0.
        if result.get("total_shares", 0) == 0:
            is_corp = result["is_corp"]
            if not is_corp:
                base = result.get("capital", 0) or result.get("authorized_capital", 0)
                if base > 0:
                    for d in result.get("directors", []):
                        shares = d.get("shares") or 0
                        d["ratio"] = round(shares / base, 6)

    return result


# ── ronnywang (primary) ───────────────────────────────────────────────────────

async def _fetch_ronny(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(RONNY_SEARCH, params={"q": name}, timeout=15.0)
        resp.raise_for_status()
        body = resp.json()

        hits = body.get("data", [])
        if not hits:
            return None

        # Prefer exact name match (full or short name without suffix), fall back to first hit
        def _short(n: str) -> str:
            for sfx in ("股份有限公司", "有限公司"):
                if n.endswith(sfx):
                    return n[:-len(sfx)]
            return n

        row = next(
            (h for h in hits
             if h.get("公司名稱") == name or _short(h.get("公司名稱", "")) == name),
            hits[0],
        )

        tax_id = row.get("統一編號", "")
        representative = row.get("代表人姓名", "")
        address = row.get("公司所在地", "")
        capital = _parse_int(row.get("實收資本額(元)") or "0")
        par_value = _parse_int(row.get("每股金額(元)") or "0")
        total_shares = _parse_int(row.get("已發行股份總數(股)") or "0")

        # When total_shares is missing (有限公司 or 股份有限公司 without share data),
        # ratio will be recalculated later using authorized_capital once GCIS is merged.
        directors = [
            {
                "name": d.get("姓名", ""),
                "title": d.get("職稱", ""),
                "representative_of": _parse_representative_of_name(d.get("所代表法人", "")),
                "representative_of_tax_id": _parse_representative_of_tax_id(d.get("所代表法人", "")),
                "shares": _parse_int(d.get("出資額", "0")),
                "ratio": round(_parse_int(d.get("出資額", "0")) / total_shares, 6)
                if total_shares > 0 else None,
            }
            for d in row.get("董監事名單", [])
        ]

        return {
            "matched_name": row.get("公司名稱", ""),
            "tax_id": tax_id,
            "representative": representative,
            "capital": capital,
            "address": address,
            "par_value": par_value,
            "total_shares": total_shares,
            "directors": directors,
        }
    except Exception:
        return None


# ── GCIS App1 (fallback, by tax_id) ──────────────────────────────────────────

async def _fetch_gcis_by_tax_id(client: httpx.AsyncClient, tax_id: str) -> dict[str, Any]:
    try:
        resp = await client.get(
            GCIS_APP1,
            params={
                "$format": "json",
                "$filter": f"Business_Accounting_NO eq '{tax_id}'",
                "$skip": "0",
                "$top": "1",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return {}
        row = data[0]
        return {
            "representative": row.get("Responsible_Name", ""),
            "capital": _parse_int(row.get("Paid_In_Capital_Amount", "0")),
            "authorized_capital": _parse_int(row.get("Capital_Stock_Amount", "0")),
            "address": row.get("Company_Location", ""),
            "setup_date": row.get("Company_Setup_Date", ""),
            "last_change_date": row.get("Change_Of_Approval_Data", ""),
            "register_org": row.get("Register_Organization_Desc", ""),
        }
    except Exception:
        return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_representative_of_name(val: Any) -> str:
    """所代表法人 is either ['統編', '公司名稱'] or an empty string."""
    if isinstance(val, list) and len(val) > 1:
        return str(val[1])
    return ""


def _parse_representative_of_tax_id(val: Any) -> str:
    """First element of 所代表法人 list is the legal entity tax_id."""
    if isinstance(val, list) and len(val) > 0 and val[0]:
        return str(val[0])
    return ""


def _parse_int(value: Any) -> int:
    if not value:
        return 0
    try:
        return int(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return 0
