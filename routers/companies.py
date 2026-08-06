import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from services import company_exporter, competitor_service, data_store, gcis_client, patent_scraper
from services.ai_deps import ai_from_headers
from services.task_progress import ProgressChannel, spawn_background as _spawn
from routers.enrichment import start_enrichment

# 競業邏輯已抽到 services/competitor_service.py（redteam #10）。競業「端點」本身
# 已搬到 routers/competitors.py；AI enrich 系列端點搬到 routers/enrichment.py。
# 這裡留下的別名僅供 ownership-graph 使用。
_short = competitor_service.short

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/companies", tags=["companies"])

_rel_channel = ProgressChannel()      # build-relationship 的 SSE 進度
_patent_channel = ProgressChannel()   # patents 的 SSE 進度


class ConfirmItem(BaseModel):
    name: str
    label: str
    industry: str | None = None
    is_new: bool
    existing_id: str | None = None
    tax_id: str | None = None


class ConfirmRequest(BaseModel):
    companies: list[ConfirmItem]
    enrich: bool = True


class IndustryUpdate(BaseModel):
    id: str
    industry: str


class BatchIndustryRequest(BaseModel):
    updates: list[IndustryUpdate]


class NameLookupRequest(BaseModel):
    names: list[str]


class ReverifyRequest(BaseModel):
    tax_ids: list[str]


class FromGraphRequest(BaseModel):
    name: str
    tax_id: str | None = None
    label: str = ""
    industry: str = ""
    source_company_id: str | None = None


class UpdateRequest(BaseModel):
    name: str | None = None
    tax_id: str | None = None
    labels: list[str] | None = None
    industries: list[str] | None = None
    group: str | None = None
    listing_status: str | None = None
    capital: int | None = None
    representative: str | None = None
    par_value: int | None = None
    no_par_value: bool | None = None
    total_shares: int | None = None
    directors: list[dict] | None = None
    address: str | None = None
    summary: str | None = None
    blurb: str | None = None
    watched: bool | None = None
    website: str | None = None


# 列表檢視（view=list）剝掉的重欄位：卡片/側欄/搜尋完全用不到，卻佔了 payload 九成
# 以上（investee_candidates 51%、summary 24%、competitors 7%、directors/materials/
# patents ~11%）。完整資料由 modal 開窗時 GET /api/companies/{id} 單筆抓。
_LIST_VIEW_STRIP = {
    "investee_candidates", "summary", "competitors", "directors",
    "materials_summary", "patents", "shareholders_analysis",
}


@router.get("")
def list_companies(industry: str | None = None, group: str | None = None,
                   sort_by: str = "capital", view: str | None = None):
    companies = data_store.get_all_companies()
    if industry:
        companies = [c for c in companies if industry in (data_store.company_industries(c))]
    if group:
        if group == "__ungrouped__":
            companies = [c for c in companies if not c.get("group")]
        else:
            companies = [c for c in companies if c.get("group") == group]
    if sort_by == "name":
        companies = sorted(companies, key=lambda c: c["name"])
    else:
        companies = sorted(companies, key=lambda c: c.get("capital", 0), reverse=True)
    if view == "list":
        slim = []
        for c in companies:
            d = {k: v for k, v in c.items() if k not in _LIST_VIEW_STRIP}
            # 前端「補齊未完成」判定需要 summary 狀態，但不需要 summary 本文——
            # 後端算好布林替代（與 isIncompleteCompany 的規則一致）
            s = (c.get("summary") or "").strip()
            d["summary_incomplete"] = (not s) or ("尚待補充" in s)
            slim.append(d)
        return slim
    return companies


@router.put("/batch-industry")
def batch_update_industry(req: BatchIndustryRequest):
    """Add an industry to multiple companies (ADD, not replace)."""
    updated = data_store.update_companies_industry({u.id: u.industry for u in req.updates})
    return {"updated": updated}


@router.delete("/batch-industry")
def batch_remove_industry(req: BatchIndustryRequest):
    """Remove an industry from multiple companies."""
    updated = data_store.remove_companies_industry({u.id: u.industry for u in req.updates})
    return {"updated": updated}


@router.get("/investee-lookup")
async def investee_lookup(name: str, tax_id: str | None = None, fuzzy: bool = False):
    """反查某法人名稱的公發母公司（直接用名稱查，不限 DB 內公司）。"""
    from services import mops_investee_client
    try:
        results = await mops_investee_client.reverse_lookup(name=name, tax_id=tax_id, fuzzy=fuzzy)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mops_investee 查詢失敗：{exc}")
    # Deduplicate by holder_id (same listed company may appear multiple times)
    seen, deduped = set(), []
    for r in results:
        if r.get("holder_id") not in seen:
            seen.add(r.get("holder_id"))
            deduped.append(r)
    return {"query": name, "count": len(deduped), "results": deduped}


@router.post("/name-lookup")
async def lookup_company_names(req: NameLookupRequest):
    """Search Ronny API for each name and return up to 5 candidate matches.

    Each item: {input, matches, rejected}
    rejected=True means Ronny found the company but GCIS confirmed it is dissolved.
    """
    names = [n.strip() for n in req.names if n.strip()]
    tasks = [gcis_client.search_company_matches(n) for n in names]
    results = await asyncio.gather(*tasks)
    return [
        {
            "input": n,
            "matches": r["matches"],
            "rejected": r.get("rejected", False),
            "not_found": r.get("not_found", False),
            "suggestions": r.get("suggestions", []),
        }
        for n, r in zip(names, results)
    ]


@router.post("/reverify-status")
async def reverify_status(req: ReverifyRequest):
    """Re-check GCIS registration status for tax_ids that timed out (驗證逾時).

    Called by the frontend in the background after name-lookup so 「驗證逾時」
    rows resolve on their own without the user manually retrying.
    Returns tax_id -> {status, is_dissolved, is_api_error, is_unverified}.
    """
    tax_ids = [t.strip() for t in req.tax_ids if t and t.strip()]
    return await gcis_client.reverify_statuses(tax_ids)


@router.get("/{company_id}/patents")
async def patent_stream(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    async def _run():
        with _patent_channel.session(company_id) as ev:
            try:
                async def push(evt):
                    ev.progress(evt.get("message", ""))

                patents = await patent_scraper.scrape_company_patents(company, push)
                data_store.update_company(company_id, {"patents": patents})
                ev.done(True, patents=patents)
            except Exception as e:
                ev.error(str(e))

    return StreamingResponse(
        _patent_channel.stream(
            company_id, lambda: _spawn(_run()),
            max_ticks=7200, terminal=("done", "error"), keepalive=True,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{company_id}/export")
async def export_company(company_id: str, format: str = Query("docx", pattern="^(docx|pdf)$"),
                         provenance: bool = Query(False)):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    raw_name = company.get("name", company_id)[:50]
    encoded  = quote(raw_name, safe="")

    # 大股東區塊需要公發公司反查資料（與 modal 一致）；查不到不阻擋匯出
    holders = None
    try:
        holders = await _lookup_investee_holders(company)
    except Exception:
        log.warning("export: investee-holders 查詢失敗，大股東表將略過", exc_info=True)

    if format == "pdf":
        data = company_exporter.build_pdf(company, holders, provenance=provenance)
        return Response(
            content=data,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}.pdf"},
        )
    else:
        data = company_exporter.build_docx(company, holders, provenance=provenance)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}.docx"},
        )


@router.get("/{company_id}")
def get_company(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


async def _lookup_investee_holders(company: dict, fuzzy: bool = False) -> dict:
    """反查哪些公發公司揭露持有此公司股份，回傳去重後（每 holder+category 取最新一期）結果。"""
    from services import mops_investee_client
    results = await mops_investee_client.reverse_lookup(
        name=company["name"],
        tax_id=company.get("tax_id") or None,
        fuzzy=fuzzy,
    )
    # 每個 (holder_id, category) 只保留最新一期
    latest: dict = {}
    for r in results:
        key = (r.get("holder_id"), r.get("category"))
        if key not in latest or r.get("as_of_date", "") > latest[key].get("as_of_date", ""):
            latest[key] = r
    deduped = sorted(latest.values(), key=lambda r: r.get("as_of_date", ""), reverse=True)
    return {"query": company["name"], "count": len(deduped),
            "total_shares": company.get("total_shares") or 0, "results": deduped}


@router.get("/{company_id}/investee-holders")
async def get_investee_holders(company_id: str, fuzzy: bool = False):
    """反查哪些公發公司在財報中揭露持有此公司的股份（串接 mops_investee）。"""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    try:
        return await _lookup_investee_holders(company, fuzzy=fuzzy)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mops_investee 查詢失敗：{exc}")


@router.post("/confirm")
async def confirm_companies(req: ConfirmRequest, ai: dict = Depends(ai_from_headers)):
    saved_ids: list[str] = []
    enriching: list[str] = []

    for item in req.companies:
        data_store.add_label(item.label)

        if item.is_new:
            company = data_store.create_company(item.name, item.label, item.industry, item.tax_id or "")
            saved_ids.append(company["id"])
            if req.enrich and start_enrichment(company["id"], **ai):
                enriching.append(company["id"])
        else:
            if item.existing_id:
                updated = data_store.add_label_to_company(item.existing_id, item.label)
                if updated is not None:
                    saved_ids.append(item.existing_id)
                    if req.enrich and start_enrichment(item.existing_id, **ai):
                        enriching.append(item.existing_id)

    return {"saved": len(saved_ids), "saved_ids": saved_ids, "enriching": enriching}


@router.put("/{company_id}")
def update_company(company_id: str, req: UpdateRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None or k == "watched"}
    company = data_store.update_company(company_id, updates)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.delete("/{company_id}")
def delete_company(company_id: str):
    ok = data_store.delete_company(company_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Company not found")
    return {"deleted": company_id}


@router.get("/{company_id}/build-relationship")
async def build_relationship_stream(company_id: str, director_index: int | None = None):
    """SSE stream that builds the relationship graph for a company.

    If `director_index` is omitted, automatically picks the largest legal-entity
    director. Otherwise uses the director at that index in the company's directors
    list (allows users to manually choose any director — natural person or legal entity).
    """
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    return StreamingResponse(
        _rel_channel.stream(
            company_id, lambda: _spawn(_build_relationship(company_id, director_index)),
            max_ticks=600, interval=0.4,
        ),
        media_type="text/event-stream",
    )


@router.get("/{company_id}/ownership-graph")
def get_ownership_graph(company_id: str):
    """Return Cytoscape-friendly nodes/edges for the company's relationship graph."""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    rel = company.get("relationship_graph") or {}
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_node_ids: set[str] = set()

    def add_node(node_id: str, label: str, role: str, **extra):
        if node_id in seen_node_ids:
            return
        seen_node_ids.add(node_id)
        nodes.append({"data": {"id": node_id, "label": label, "role": role, **extra}})

    # Center: this company
    self_id = f"c:{company['id']}"
    add_node(
        self_id,
        _short(company["name"]),
        "self",
        company_id=company["id"],
        in_db=True,
        tax_id=company.get("tax_id", ""),
        listing_status=company.get("listing_status", ""),
    )

    parent = rel.get("parent")
    parent_node_id = None
    if parent and (parent.get("name") or parent.get("tax_id")):
        kind = parent.get("kind") or ("person" if not parent.get("tax_id") else "legal_entity")
        parent_node_id = f"p:{parent.get('tax_id') or parent.get('name')}"
        add_node(
            parent_node_id,
            _short(parent.get("name") or "(未知)"),
            "parent",
            kind=kind,
            tax_id=parent.get("tax_id", ""),
            in_db=bool(parent.get("company_id")),
            company_id=parent.get("company_id") or "",
            listing_status=parent.get("listing_status", ""),
            title=parent.get("title", ""),
        )
        # parent → self
        edges.append({"data": {
            "id": f"e:{parent_node_id}->{self_id}",
            "source": parent_node_id,
            "target": self_id,
            "ratio": parent.get("ratio") or 0,
            "via_director": parent.get("via_director") or "",
        }})

    # Siblings: parent → other companies
    if parent_node_id:
        for s in (rel.get("siblings") or []):
            sib_id = f"s:{s.get('tax_id') or s.get('name')}"
            add_node(
                sib_id,
                _short(s.get("name") or "(未知)"),
                "sibling",
                tax_id=s.get("tax_id", ""),
                in_db=bool(s.get("in_db")),
                company_id=s.get("company_id") or "",
                ratio=s.get("ratio") or 0,
                listing_status=s.get("listing_status", ""),
                title=s.get("title", ""),
            )
            edges.append({"data": {
                "id": f"e:{parent_node_id}->{sib_id}",
                "source": parent_node_id,
                "target": sib_id,
                "ratio": s.get("ratio") or 0,
                "via_director": s.get("via_director") or s.get("title") or "",
            }})

    return {
        "nodes": nodes,
        "edges": edges,
        "last_updated": rel.get("last_updated", ""),
        "note": rel.get("note", ""),
    }


@router.post("/from-graph")
async def add_company_from_graph(req: FromGraphRequest, ai: dict = Depends(ai_from_headers)):
    """Create a new company entry from a graph node and start enrichment.

    Returns 200 with `existed: true` if the company already exists (matched by tax_id or name).
    """
    # 圖節點名常帶「（股號）」（AI 競業池慣用寫法）——先清再比對/建檔，
    # 否則髒名入庫會讓 GCIS/TWSE 全查不到（美琪瑪（4721）事件）
    name = data_store.clean_company_name(req.name)
    tax_id = (req.tax_id or "").strip()
    if not name and not tax_id:
        raise HTTPException(status_code=400, detail="name or tax_id required")

    existing = data_store.find_company_by_name_or_tax_id(name, tax_id)
    if existing:
        return {"existed": True, "company_id": existing["id"], "name": existing["name"]}

    label = (req.label or "").strip()
    industry = (req.industry or "").strip()
    if label:
        data_store.add_label(label)

    company = data_store.create_company(name, label, industry)
    if tax_id:
        data_store.update_company(company["id"], {"tax_id": tax_id})

    start_enrichment(company["id"], **ai)

    return {"existed": False, "company_id": company["id"], "name": company["name"]}


def _build_company_index(companies: list[dict]) -> dict:
    by_tax: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for c in companies:
        if c.get("tax_id"):
            by_tax[c["tax_id"]] = c
        if c.get("name"):
            by_name[data_store.normalize_company_name(c["name"])] = c
    return {"by_tax": by_tax, "by_name": by_name}


def _lookup_local(index: dict, name: str, tax_id: str) -> dict | None:
    if tax_id and tax_id in index["by_tax"]:
        return index["by_tax"][tax_id]
    if name:
        normalized = data_store.normalize_company_name(name)
        if normalized in index["by_name"]:
            return index["by_name"][normalized]
    return None


async def _build_relationship(company_id: str, director_index: int | None = None) -> None:
    with _rel_channel.session(company_id) as ev:
        try:
            companies_snapshot = data_store.get_all_companies()
            company = next((c for c in companies_snapshot if c["id"] == company_id), None)
            if not company:
                ev.done()
                return

            directors = company.get("directors") or []

            # If no explicit index, fall back to the last anchor used (so 「重新分析」 keeps the same one)
            if director_index is None:
                director_index = (company.get("relationship_graph") or {}).get("director_index")

            target_director: dict | None = None
            if director_index is not None:
                if 0 <= director_index < len(directors):
                    target_director = directors[director_index]
                else:
                    ev.progress(f"董事索引 {director_index} 超出範圍，改用自動選擇")
                    director_index = None
            if target_director is None:
                ev.progress("分析董監事名單，自動選擇最大股法人代表…")
                target_director = gcis_client.pick_largest_legal_director(directors)
                if target_director is not None:
                    director_index = directors.index(target_director)

            if not target_director:
                ev.progress("此公司董監事中無法人代表，且未指定董事，無關係可分析")
                data_store.update_company(company_id, {"relationship_graph": {
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                    "director_index": None,
                    "parent": None,
                    "siblings": [],
                    "note": "此公司董監事中無法人代表",
                }})
                ev.done()
                return

            company_index = _build_company_index(companies_snapshot)
            # Director represents a legal entity if either:
            #  (a) `representative_of` is set (natural person as legal-entity proxy), or
            #  (b) the director's own name looks like a company (法人股東直接任董事)
            is_legal = bool((target_director.get("representative_of") or "").strip()) \
                or _looks_like_company_name(target_director.get("name") or "")

            if is_legal:
                result = await _build_legal_entity_anchor(company, target_director, company_index, ev.progress)
            else:
                result = await _build_person_anchor(company, target_director, company_index, ev.progress)

            if result is not None:
                parent_node, siblings, note = result
                data_store.update_company(company_id, {"relationship_graph": {
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                    "director_index": director_index,
                    "parent": parent_node,
                    "siblings": siblings,
                    "note": note,
                }})
                ev.progress("關係資料補強完成")

            ev.done()
        except Exception as e:
            ev.progress(f"分析失敗:{e}")
            ev.done()


def _looks_like_company_name(name: str) -> bool:
    """The director's own name field looks like a legal entity (sits on board directly)."""
    if not name:
        return False
    keywords = ("股份有限公司", "有限公司", "公司", "企業", "集團", "銀行", "工廠", "合夥", "基金會", "協會")
    return any(k in name for k in keywords)


async def _build_legal_entity_anchor(
    company: dict, target_director: dict, company_index: dict, push,
) -> tuple[dict, list[dict], str]:
    rep_name = (target_director.get("representative_of") or "").strip()
    rep_tax_id = (target_director.get("representative_of_tax_id") or "").strip()
    # Fallback: director's name itself is the legal entity (no separate rep)
    if not rep_name:
        rep_name = (target_director.get("name") or "").strip()
        rep_tax_id = ""
    push(f"分析錨點為法人:{rep_name}(董事 {target_director.get('name','')},持股 {(target_director.get('ratio') or 0) * 100:.2f}%)")
    push("並行查詢母法人基本資料與其投資的所有公司…")

    parent_data, online_subs = await asyncio.gather(
        gcis_client.fetch_parent_entity_data(rep_name, rep_tax_id),
        gcis_client.fetch_subsidiaries_of_legal_entity(rep_name, rep_tax_id),
        return_exceptions=True,
    )
    if isinstance(parent_data, Exception):
        push(f"母法人查詢失敗:{parent_data},僅以名稱繼續")
        parent_data = {"name": rep_name, "tax_id": rep_tax_id}
    if isinstance(online_subs, Exception):
        push(f"線上反查失敗:{online_subs}")
        online_subs = []

    parent_tax_id = parent_data.get("tax_id") or rep_tax_id
    parent_name = parent_data.get("name") or rep_name
    parent_in_db = _lookup_local(company_index, parent_name, parent_tax_id)

    parent_node = {
        "kind": "legal_entity",
        "name": parent_name,
        "tax_id": parent_tax_id,
        "via_director": target_director.get("name", ""),
        "ratio": target_director.get("ratio") or 0,
        "listing_status": parent_data.get("listing_status", ""),
        "company_id": parent_in_db["id"] if parent_in_db else "",
        "in_db": bool(parent_in_db),
        "data_source": "ronny",
    }

    siblings = _build_siblings(company, online_subs, company_index, has_via_director=True, source="ronny_fund")
    push(f"找到 {len(siblings)} 家兄弟公司({sum(1 for s in siblings if s['in_db'])} 家已在本地)")
    return parent_node, siblings, "兄弟公司清單來自 Ronny /api/fund 反向查詢，已標註本地收錄狀態。"


async def _build_person_anchor(
    company: dict, target_director: dict, company_index: dict, push,
) -> tuple[dict, list[dict], str] | None:
    person_name = (target_director.get("name") or "").strip()
    if not person_name:
        push("董事姓名為空，無法分析")
        return None

    push(f"分析錨點為自然人:{person_name}(職稱 {target_director.get('title','—')})")
    push("線上查詢此人擔任董監事的所有公司…")
    try:
        related = await gcis_client.fetch_companies_of_person(person_name)
    except Exception as e:
        push(f"反查失敗:{e}")
        related = []

    siblings = _build_siblings(company, related, company_index, has_via_director=False, source="ronny_name")
    push(f"找到 {len(siblings)} 家相關公司({sum(1 for s in siblings if s['in_db'])} 家已在本地)")

    parent_node = {
        "kind": "person",
        "name": person_name,
        "tax_id": "",
        "via_director": person_name,
        "ratio": target_director.get("ratio") or 0,
        "title": target_director.get("title", ""),
        "listing_status": "",
        "company_id": "",
        "in_db": False,
        "data_source": "ronny",
    }
    return parent_node, siblings, "⚠ 來自 Ronny /api/name 反查;同名同姓無法區分,請依公司資料判別是否為同一人。"


def _build_siblings(
    company: dict, raw: list[dict], company_index: dict, has_via_director: bool, source: str,
) -> list[dict]:
    self_tax_id = (company.get("tax_id") or "").strip()
    self_name = company.get("name") or ""
    out: list[dict] = []
    for s in raw:
        s_tax_id = (s.get("tax_id") or "").strip()
        s_name = s.get("name") or ""
        if s_tax_id and s_tax_id == self_tax_id:
            continue
        if not s_tax_id and s_name == self_name:
            continue
        local = _lookup_local(company_index, s_name, s_tax_id)
        item = {
            "name": s_name,
            "tax_id": s_tax_id,
            "company_id": local["id"] if local else "",
            "in_db": bool(local),
            "ratio": s.get("ratio") or 0,
            "shares": s.get("shares") or 0,
            "listing_status": local.get("listing_status", "") if local else "",
            "data_source": source,
        }
        if has_via_director:
            item["via_director"] = s.get("via_director") or ""
        else:
            item["title"] = s.get("title", "")
            item["represents_legal_entity"] = s.get("represents_legal_entity", "")
        out.append(item)
    return out
