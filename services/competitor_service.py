"""競業（competitor）相關邏輯，從 routers/companies.py 抽出（redteam #10）。

只依賴 data_store，不 import router，避免循環匯入。
router 端以模組別名接回原本的 `_short` / `_gather_competitor_context` 等名稱，
呼叫點不需改動、行為與抽出前完全一致。
"""
import re

from . import data_store

COMPETITION_TYPES = {"正面競業", "替代路徑", "側翼潛入", "垂直整合"}


def short(name: str) -> str:
    for sfx in ("股份有限公司", "有限公司"):
        if name.endswith(sfx):
            return name[: -len(sfx)]
    return name


def gather_competitor_context(company_id: str, company_name: str) -> dict:
    """
    Return two layers of competitor context for prompt injection:
      direct   – companies in DB that explicitly list this company as their competitor
      extended – those companies' own DB-linked competitors (one hop), for AI reference only
    """
    name_key = short(company_name)
    all_cos = data_store.get_all_companies()
    by_id = {c["id"]: c for c in all_cos}

    # Layer 1: direct (companies that list this company as competitor)
    direct: list[dict] = []
    seen_direct: set[str] = set()
    for other in all_cos:
        if other["id"] == company_id:
            continue
        for comp in (other.get("competitors") or []):
            if comp.get("company_id") == company_id or short(comp.get("name", "")) == name_key:
                if other["id"] not in seen_direct:
                    seen_direct.add(other["id"])
                    direct.append({
                        "name": other["name"],
                        "id": other["id"],
                        "blurb": other.get("blurb") or "",
                        "listing_status": other.get("listing_status") or "",
                    })
                break

    # Layer 2: extended – DB-linked competitors of direct companies (one hop)
    extended: list[dict] = []
    seen_extended: set[str] = set()
    for direct_co in direct:
        source = by_id.get(direct_co["id"])
        if not source:
            continue
        for comp in (source.get("competitors") or []):
            cid = comp.get("company_id")
            if not cid or cid == company_id or cid in seen_direct or cid in seen_extended:
                continue
            ext = by_id.get(cid)
            if not ext:
                continue
            seen_extended.add(cid)
            extended.append({
                "name": ext["name"],
                "id": cid,
                "blurb": ext.get("blurb") or "",
                "listing_status": ext.get("listing_status") or "",
                "via": direct_co["name"],
            })

    return {"direct": direct, "extended": extended}


def resolve_competitor_ids(competitors: list[dict]) -> list[dict]:
    """Fill in company_id for competitors that are already in the DB."""
    all_cos = data_store.get_all_companies()
    # Index by both stored name and short name so full/short mismatches resolve correctly
    name_to_id: dict[str, str] = {}
    for c in all_cos:
        name_to_id[c["name"]] = c["id"]
        name_to_id[short(c["name"])] = c["id"]
    for comp in competitors:
        name = comp.get("name", "")
        comp["company_id"] = name_to_id.get(name) or name_to_id.get(short(name)) or None
    return competitors


def backlink_competitor(new_id: str, new_name: str) -> None:
    """When a new company is added, update other companies' competitors[].company_id."""
    new_key = short(new_name)
    for co in data_store.get_all_companies():
        if co["id"] == new_id:
            continue
        comps = co.get("competitors")
        if not comps:
            continue
        updated = False
        for comp in comps:
            if comp.get("company_id") is None and short(comp.get("name", "")) == new_key:
                comp["company_id"] = new_id
                updated = True
        if updated:
            data_store.update_company(co["id"], {"competitors": comps})


def insert_competitor_row(summary: str, row: str) -> str:
    """Append a row to the markdown 競業分析 table in summary. Returns unchanged
    summary if no such table is found."""
    lines = summary.split("\n")
    in_section = False
    last_row_idx = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if re.match(r"^##\s+競業分析", s):
            in_section = True
            continue
        if in_section and re.match(r"^##\s+", s):
            break
        if in_section and s.startswith("|") and s.endswith("|") and not re.match(r"^\|[\s\-|]+\|$", s):
            last_row_idx = i
    if last_row_idx == -1:
        return summary
    lines.insert(last_row_idx + 1, row)
    return "\n".join(lines)


def remove_competitor_row(summary: str, name: str) -> str:
    """Remove the competitor row whose first cell (公司名稱) equals `name` from the
    markdown 競業分析 table. Never touches the 本案 row. Returns the summary
    unchanged if no matching row is found."""
    target = (name or "").strip()
    if not target:
        return summary
    lines = summary.split("\n")
    in_section = False
    for i, line in enumerate(lines):
        s = line.strip()
        if re.match(r"^##\s+競業分析", s):
            in_section = True
            continue
        if in_section and re.match(r"^##\s+", s):
            break
        if in_section and s.startswith("|") and s.endswith("|") and not re.match(r"^\|[\s\-|]+\|$", s):
            cells = [c.strip() for c in s.split("|")[1:-1]]
            if not cells:
                continue
            first = cells[0]
            if "（本案）" in first:
                continue
            if first == target:
                del lines[i]
                return "\n".join(lines)
    return summary
