"""
Fetch company data from ronnywang g0v company search API.
https://company.g0v.ronny.tw/api/search?q={company_name}

Single request returns: tax_id, capital, representative, address,
par_value, total_shares, and full director list.
Falls back to GCIS App1 API (by tax_id) when ronnywang returns no match.
Listing status (上市/上櫃/興櫃/創新板/非公發) is resolved from TWSE/TPEX open APIs.
創櫃板: TPEX 尚未提供公開 JSON API，暫不支援自動辨識。
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx

RONNY_SEARCH = "https://company.g0v.ronny.tw/api/search"
GCIS_APP1 = (
    "https://data.gcis.nat.gov.tw/od/data/api/"
    "5F64D864-61CB-4D0D-8AD9-492047CC1EA6"
)
GCIS_DIRECTOR_API = (
    "https://data.gcis.nat.gov.tw/od/data/api/"
    "6BBA2268-1367-4B42-9CCA-BC17499EBE8C"
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


async def fetch_company_name_by_tax_id(tax_id: str) -> str:
    """Return the official company name for a given tax ID, or empty string if not found."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.get(f"https://company.g0v.ronny.tw/api/id/{tax_id}")
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", {})
            return data.get("公司名稱", "") or data.get("Company_Name", "")
        except Exception:
            pass
        # fallback: GCIS App1
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
            if data:
                return data[0].get("Company_Name", "") or data[0].get("公司名稱", "")
        except Exception:
            pass
    return ""


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
    }

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        await _ensure_listing_cache(client)

        ronny = await _fetch_ronny(client, name)
        if ronny:
            result.update(ronny)

        # Always fetch GCIS for authorized_capital (Capital_Stock_Amount) when we have tax_id
        tax_id = result.get("tax_id", "")
        if tax_id:
            gcis = await _fetch_gcis_by_tax_id(client, tax_id)
            if gcis.get("authorized_capital"):
                result["authorized_capital"] = gcis["authorized_capital"]
            if not ronny:
                for k in ("representative", "capital", "address"):
                    if gcis.get(k):
                        result[k] = gcis[k]

        result["listing_status"] = _resolve_listing_status(tax_id, name)

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

        # Prefer exact name match, fall back to first hit
        row = next((h for h in hits if h.get("公司名稱") == name), hits[0])

        tax_id = row.get("統一編號", "")
        representative = row.get("代表人姓名", "")
        address = row.get("公司所在地", "")
        capital = _parse_int(row.get("實收資本額(元)", "0"))
        par_value = _parse_int(row.get("每股金額(元)", "0"))
        total_shares = _parse_int(row.get("已發行股份總數(股)", "0"))

        directors = [
            {
                "name": d.get("姓名", ""),
                "title": d.get("職稱", ""),
                "representative_of": _parse_representative_of(d.get("所代表法人", "")),
                "shares": _parse_int(d.get("出資額", "0")),
                "ratio": round(_parse_int(d.get("出資額", "0")) / total_shares, 6)
                if total_shares > 0 else 0.0,
            }
            for d in row.get("董監事名單", [])
        ]

        return {
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
        }
    except Exception:
        return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_representative_of(val: Any) -> str:
    """所代表法人 is either ['統編', '公司名稱'] or an empty string."""
    if isinstance(val, list) and len(val) > 1:
        return str(val[1])
    return ""


def _parse_int(value: Any) -> int:
    if not value:
        return 0
    try:
        return int(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return 0
