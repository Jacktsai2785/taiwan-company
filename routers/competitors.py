import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services import competitor_service, data_store, report_generator
from services.ai_deps import ai_from_headers

# 競業邏輯本身在 services/competitor_service.py（redteam #10）。這裡是它的路由層：
# 原本連同其他公司端點一起塞在 routers/companies.py，佔了該檔約 45% 的行數，
# 也是 workflow 圖上 companies.py 扇出最多條依賴線的主因，故拆成獨立router。
_short = competitor_service.short
_resolve_competitor_ids = competitor_service.resolve_competitor_ids
_insert_competitor_row = competitor_service.insert_competitor_row
_remove_competitor_row = competitor_service.remove_competitor_row
_COMPETITION_TYPES = competitor_service.COMPETITION_TYPES

router = APIRouter(prefix="/api/companies", tags=["competitors"])


@router.get("/{company_id}/competitor-graph")
def get_competitor_graph(company_id: str):
    """Return Cytoscape-friendly nodes/edges for the competitor landscape."""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(node_id: str, label: str, role: str, **extra):
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"data": {"id": node_id, "label": label, "role": role, **extra}})

    def add_edge(src: str, tgt: str):
        eid = "ce:" + "-".join(sorted([src, tgt]))
        if eid in seen:
            return
        seen.add(eid)
        edges.append({"data": {"id": eid, "source": src, "target": tgt, "type": "competition"}})

    self_id = f"c:{company_id}"
    add_node(self_id, _short(company["name"]), "self",
             company_id=company_id, in_db=True,
             listing_status=company.get("listing_status", ""))

    # Primary competitors listed in this company's memo
    for comp in (company.get("competitors") or []):
        name = comp.get("name", "")
        cid  = comp.get("company_id")
        # 競業條目來自備忘錄表格解析，company_id 多半為 None；使用者事後把該公司
        # 加入清單也不會回填舊條目。這裡即時以名稱（去掉品牌附註括號）／統編比對
        # 現有公司，讓「已收錄」狀態與點擊行為一致（避免「顯示未收錄但點得開」）。
        if not cid:
            lookup_name = re.sub(r"（[^）]*）", "", name).strip()
            hit = data_store.find_company_by_name_or_tax_id(lookup_name, comp.get("tax_id") or "")
            if hit:
                cid = hit["id"]
        node_id = f"comp:{cid}" if cid else f"comp:name:{name}"
        in_db = bool(cid)
        add_node(node_id, _short(name), "competitor",
                 name=name, in_db=in_db, company_id=cid or "",
                 listing_status=comp.get("listing_status", ""),
                 core_biz=comp.get("core_biz", ""))
        add_edge(self_id, node_id)

    # Reverse lookup: companies in DB that list this company as their competitor
    self_name_key = _short(company["name"])
    for other in data_store.get_all_companies():
        if other["id"] == company_id:
            continue
        for comp in (other.get("competitors") or []):
            if comp.get("company_id") == company_id or _short(comp.get("name", "")) == self_name_key:
                node_id = f"comp:{other['id']}"
                add_node(node_id, _short(other["name"]), "competitor",
                         name=other["name"], in_db=True, company_id=other["id"],
                         listing_status=other.get("listing_status", ""),
                         core_biz="")
                add_edge(self_id, node_id)
                break

    return {"nodes": nodes, "edges": edges}


@router.post("/symmetrize-competitors")
def symmetrize_competitors():
    """
    Make competitor relationships bidirectional.
    For every A → B link (B has company_id), ensure B's competitors[] also contains A.
    Synthetic reverse entries use the company's blurb as core_biz.
    """
    all_cos = data_store.get_all_companies()
    by_id = {c["id"]: c for c in all_cos}

    working: dict[str, list[dict]] = {}
    changed: set[str] = set()

    def get_working(cid: str) -> list[dict]:
        if cid not in working:
            working[cid] = [dict(c) for c in (by_id[cid].get("competitors") or [])]
        return working[cid]

    added = 0
    for co in all_cos:
        a_id = co["id"]
        a_key = _short(co["name"])
        for comp in (co.get("competitors") or []):
            b_id = comp.get("company_id")
            if not b_id or b_id not in by_id or b_id == a_id:
                continue
            b_comps = get_working(b_id)
            already = any(
                c.get("company_id") == a_id or _short(c.get("name", "")) == a_key
                for c in b_comps
            )
            if not already:
                b_comps.append({
                    "name": co["name"],
                    "tax_id": co.get("tax_id") or None,
                    "company_id": a_id,
                    "core_biz": co.get("blurb") or "",
                    "listing_status": co.get("listing_status") or "非公發",
                })
                changed.add(b_id)
                added += 1

    for cid in changed:
        data_store.update_company(cid, {"competitors": working[cid]})

    return {"updated_companies": len(changed), "added_links": added}


@router.post("/relink-competitors")
def relink_competitors():
    """
    Re-resolve company_id for ALL competitors entries that currently have company_id=null.
    Use this after fixing name-normalization bugs or adding new companies in bulk.
    """
    all_cos = data_store.get_all_companies()
    name_to_id: dict[str, str] = {}
    for c in all_cos:
        name_to_id[c["name"]] = c["id"]
        name_to_id[_short(c["name"])] = c["id"]
    updated_companies = 0
    resolved_links = 0
    for co in all_cos:
        comps = co.get("competitors")
        if not comps:
            continue
        changed = False
        for comp in comps:
            if comp.get("company_id") is not None:
                continue
            n = comp.get("name", "")
            cid = name_to_id.get(n) or name_to_id.get(_short(n)) or None
            if cid:
                comp["company_id"] = cid
                changed = True
                resolved_links += 1
        if changed:
            data_store.update_company(co["id"], {"competitors": comps})
            updated_companies += 1
    return {"updated_companies": updated_companies, "resolved_links": resolved_links}


@router.post("/backfill-competitors")
def backfill_competitors():
    """
    One-shot: parse existing summaries that have no competitors field yet,
    fill structured competitors data, and resolve company_id cross-references.
    Returns a summary of how many companies were updated.
    """
    all_cos = data_store.get_all_companies()
    name_to_id: dict[str, str] = {}
    for c in all_cos:
        name_to_id[c["name"]] = c["id"]
        name_to_id[_short(c["name"])] = c["id"]
    updated = 0
    for co in all_cos:
        if co.get("competitors") is not None:
            continue
        summary = co.get("summary") or ""
        if not summary:
            continue
        comps = report_generator._parse_competitor_table(summary)
        if not comps:
            continue
        for comp in comps:
            n = comp.get("name", "")
            comp["company_id"] = name_to_id.get(n) or name_to_id.get(_short(n)) or None
        data_store.update_company(co["id"], {"competitors": comps})
        updated += 1
    return {"updated": updated, "total": len(all_cos)}


class AddCompetitorRequest(BaseModel):
    name: str
    competition_type: str


@router.post("/{company_id}/competitors/add")
async def add_competitor(company_id: str, req: AddCompetitorRequest, ai: dict = Depends(ai_from_headers)):
    """Manually add a competitor: AI researches it (WebSearch) and fills 核心業務/
    差異化/上市狀態; 競業類型 is the user's choice. Inserted into the 競業分析 table."""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    name = (req.name or "").strip()
    ctype = (req.competition_type or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="請輸入競業公司名稱")
    if ctype not in _COMPETITION_TYPES:
        raise HTTPException(status_code=422, detail="競業類型不正確")
    if not (company.get("summary") or "").strip():
        raise HTTPException(status_code=422, detail="本案尚無公司簡介，請先生成簡介再新增競業")

    analysis = await report_generator.analyze_competitor(company, name, ctype, **ai)
    # use the AI-resolved full legal name (matches other rows + enables company link);
    # listing is already resolved in analyze_competitor, so don't run the downgrading
    # _fix_competitor_listing here (it would clobber short/unmatched names to 非公發).
    row_name = analysis.get("full_name") or name
    row = f"| {row_name} | {analysis['core_biz']} | {analysis['differentiation']} | {analysis['listing']} | {ctype} |"
    new_summary = _insert_competitor_row(company.get("summary", ""), row)
    if new_summary == company.get("summary", ""):
        raise HTTPException(status_code=422, detail="找不到競業分析表格，請先生成公司簡介")

    competitors = _resolve_competitor_ids(report_generator._parse_competitor_table(new_summary))
    data_store.update_company(company_id, {"summary": new_summary, "competitors": competitors})
    return {"summary": new_summary, "competitors": competitors}


class RemoveCompetitorRequest(BaseModel):
    name: str


@router.post("/{company_id}/competitors/remove")
def remove_competitor(company_id: str, req: RemoveCompetitorRequest):
    """Manually delete a competitor row from the 競業分析 table. Identified by the
    raw first-cell text (公司名稱). Re-parses the structured competitors list and
    persists. Does not call AI."""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="請指定要刪除的競業名稱")
    new_summary = _remove_competitor_row(company.get("summary", ""), name)
    if new_summary == company.get("summary", ""):
        raise HTTPException(status_code=422, detail="找不到要刪除的競業列")

    competitors = _resolve_competitor_ids(report_generator._parse_competitor_table(new_summary))
    data_store.update_company(company_id, {"summary": new_summary, "competitors": competitors})
    return {"summary": new_summary, "competitors": competitors}
