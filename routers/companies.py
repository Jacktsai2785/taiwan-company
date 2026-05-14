import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from services import company_extractor, company_exporter, data_store, gcis_client, report_generator, patent_scraper
from services.ai_deps import ai_from_headers, ai_from_query

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/companies", tags=["companies"])

_progress: dict[str, list[dict]] = {}
_running: set[str] = set()
_rel_progress: dict[str, list[dict]] = {}
_rel_running: set[str] = set()
_deep_progress: dict[str, list[dict]] = {}
_deep_running: set[str] = set()
_patent_progress: dict[str, list[dict]] = {}
_patent_running: set[str] = set()


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


class EnrichBatchRequest(BaseModel):
    company_ids: list[str]


class SuggestIndustriesRequest(BaseModel):
    company_ids: list[str] | None = None


class IndustryUpdate(BaseModel):
    id: str
    industry: str


class BatchIndustryRequest(BaseModel):
    updates: list[IndustryUpdate]


class NameLookupRequest(BaseModel):
    names: list[str]


class FromGraphRequest(BaseModel):
    name: str
    tax_id: str | None = None
    label: str = ""
    industry: str = ""
    source_company_id: str | None = None


class UpdateRequest(BaseModel):
    name: str | None = None
    labels: list[str] | None = None
    industry: str | None = None
    group: str | None = None
    listing_status: str | None = None
    capital: int | None = None
    representative: str | None = None
    par_value: int | None = None
    total_shares: int | None = None
    directors: list[dict] | None = None
    address: str | None = None
    summary: str | None = None
    blurb: str | None = None
    watched: bool | None = None


@router.get("")
def list_companies(industry: str | None = None, group: str | None = None, sort_by: str = "capital"):
    companies = data_store.get_all_companies()
    if industry:
        companies = [c for c in companies if c.get("industry") == industry]
    if group:
        if group == "__ungrouped__":
            companies = [c for c in companies if not c.get("group")]
        else:
            companies = [c for c in companies if c.get("group") == group]
    if sort_by == "name":
        companies = sorted(companies, key=lambda c: c["name"])
    else:
        companies = sorted(companies, key=lambda c: c.get("capital", 0), reverse=True)
    return companies


@router.put("/batch-industry")
def batch_update_industry(req: BatchIndustryRequest):
    """Apply industry updates to multiple companies in a single read-modify-write to avoid races."""
    updated = data_store.update_companies_industry({u.id: u.industry for u in req.updates})
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
    """Search Ronny API for each name and return up to 5 candidate matches."""
    names = [n.strip() for n in req.names if n.strip()]
    tasks = [gcis_client.search_company_matches(n) for n in names]
    results = await asyncio.gather(*tasks)
    return [{"input": n, "matches": m} for n, m in zip(names, results)]


@router.post("/suggest-industries")
async def suggest_industries(req: SuggestIndustriesRequest, ai: dict = Depends(ai_from_headers)):
    """Use AI to assign each given company an industry from the existing list.

    If `company_ids` is omitted, defaults to all companies missing an industry.
    Does not write changes — caller applies via PUT.
    """
    industries = data_store.get_industries()
    if not industries:
        raise HTTPException(status_code=422, detail="尚未建立任何產業別，請先新增至少一個產業別")

    all_companies = data_store.get_all_companies()
    if req.company_ids:
        wanted = set(req.company_ids)
        targets = [c for c in all_companies if c["id"] in wanted]
    else:
        targets = [c for c in all_companies if not (c.get("industry") or "")]

    if not targets:
        return {"suggestions": {}, "industries": industries, "targets": []}

    suggestions = await company_extractor.suggest_industries_for_companies(
        targets, industries, **ai
    )
    return {
        "suggestions": suggestions,
        "industries": industries,
        "targets": [{"id": c["id"], "name": c["name"], "blurb": c.get("blurb") or ""} for c in targets],
    }


@router.get("/{company_id}/patents")
async def patent_stream(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    async def _run():
        events = _patent_progress.setdefault(company_id, [])
        try:
            async def push(evt):
                events.append(evt)

            patents = await patent_scraper.scrape_company_patents(company, push)
            data_store.update_company(company_id, {"patents": patents})
            events.append({"type": "done", "patents": patents})
        except Exception as e:
            events.append({"type": "error", "message": str(e)})
        finally:
            _patent_running.discard(company_id)

    async def event_generator():
        if company_id not in _patent_running:
            _patent_running.add(company_id)
            asyncio.create_task(_run())
        sent = 0
        for _ in range(7200):
            events = _patent_progress.get(company_id, [])
            while sent < len(events):
                yield f"data: {json.dumps(events[sent], ensure_ascii=False)}\n\n"
                sent += 1
            if events and events[-1].get("type") in ("done", "error"):
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(0.5)
        yield 'data: {"type": "done"}\n\n'
        _patent_progress.pop(company_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{company_id}/export")
def export_company(company_id: str, format: str = Query("docx", regex="^(docx|pdf)$")):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    raw_name = company.get("name", company_id)[:50]
    encoded  = quote(raw_name, safe="")

    if format == "pdf":
        data = company_exporter.build_pdf(company)
        return Response(
            content=data,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}.pdf"},
        )
    else:
        data = company_exporter.build_docx(company)
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


@router.get("/{company_id}/investee-holders")
async def get_investee_holders(company_id: str, fuzzy: bool = False):
    """反查哪些公發公司在財報中揭露持有此公司的股份（串接 mops_investee）。"""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    from services import mops_investee_client
    try:
        results = await mops_investee_client.reverse_lookup(
            name=company["name"],
            tax_id=company.get("tax_id") or None,
            fuzzy=fuzzy,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mops_investee 查詢失敗：{exc}")
    total_shares = company.get("total_shares") or 0
    return {"query": company["name"], "count": len(results), "total_shares": total_shares, "results": results}


@router.post("/confirm")
async def confirm_companies(req: ConfirmRequest, ai: dict = Depends(ai_from_headers)):
    saved_ids: list[str] = []
    enriching: list[str] = []

    for item in req.companies:
        data_store.add_label(item.label)

        if item.is_new:
            company = data_store.create_company(item.name, item.label, item.industry, item.tax_id or "")
            saved_ids.append(company["id"])
            if req.enrich:
                enriching.append(company["id"])
                _running.add(company["id"])
                asyncio.create_task(_enrich_company(company["id"], **ai))
        else:
            if item.existing_id:
                updated = data_store.add_label_to_company(item.existing_id, item.label)
                if updated:
                    saved_ids.append(item.existing_id)
                    if req.enrich and item.existing_id not in _running:
                        enriching.append(item.existing_id)
                        _running.add(item.existing_id)
                        asyncio.create_task(_enrich_company(item.existing_id, **ai))

    return {"saved": len(saved_ids), "saved_ids": saved_ids, "enriching": enriching}


@router.post("/enrich-batch")
async def enrich_batch(req: EnrichBatchRequest, ai: dict = Depends(ai_from_headers)):
    """Spawn enrichment tasks for the given company IDs (skips ones already running)."""
    known = {c["id"] for c in data_store.get_all_companies()}
    started: list[str] = []
    for cid in req.company_ids:
        if cid not in known:
            continue
        if cid in _running:
            continue
        _running.add(cid)
        asyncio.create_task(_enrich_company(cid, **ai))
        started.append(cid)
    return {"started": started}


@router.get("/enrich/{company_id}")
async def enrich_stream(company_id: str, ai: dict = Depends(ai_from_query)):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    async def event_generator():
        if company_id not in _running:
            _running.add(company_id)
            asyncio.create_task(_enrich_company(company_id, **ai))
        sent = 0
        for _ in range(3600):
            events = _progress.get(company_id, [])
            while sent < len(events):
                yield f"data: {json.dumps(events[sent], ensure_ascii=False)}\n\n"
                sent += 1
            if events and events[-1].get("type") == "done":
                break
            await asyncio.sleep(0.5)
        yield 'data: {"type": "done"}\n\n'
        _progress.pop(company_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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


@router.get("/{company_id}/deep-enrich")
async def deep_enrich_stream(company_id: str, ai: dict = Depends(ai_from_query)):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    async def event_generator():
        if company_id not in _deep_running:
            _deep_running.add(company_id)
            asyncio.create_task(_deep_enrich_company(company_id, **ai))
        sent = 0
        for _ in range(3600):
            events = _deep_progress.get(company_id, [])
            while sent < len(events):
                yield f"data: {json.dumps(events[sent], ensure_ascii=False)}\n\n"
                sent += 1
            if events and events[-1].get("type") == "done":
                break
            await asyncio.sleep(0.5)
        yield 'data: {"type": "done"}\n\n'
        _deep_progress.pop(company_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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

    async def event_generator():
        if company_id not in _rel_running:
            _rel_running.add(company_id)
            asyncio.create_task(_build_relationship(company_id, director_index))
        sent = 0
        for _ in range(600):
            events = _rel_progress.get(company_id, [])
            while sent < len(events):
                yield f"data: {json.dumps(events[sent], ensure_ascii=False)}\n\n"
                sent += 1
            if events and events[-1].get("type") == "done":
                break
            await asyncio.sleep(0.4)
        yield 'data: {"type": "done"}\n\n'
        _rel_progress.pop(company_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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
    name = (req.name or "").strip()
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

    _running.add(company["id"])
    asyncio.create_task(_enrich_company(company["id"], **ai))

    return {"existed": False, "company_id": company["id"], "name": company["name"]}


def _short(name: str) -> str:
    for sfx in ("股份有限公司", "有限公司"):
        if name.endswith(sfx):
            return name[: -len(sfx)]
    return name


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
    events: list[dict] = []
    _rel_progress[company_id] = events

    def push(msg: str):
        events.append({"type": "progress", "message": msg})

    try:
        companies_snapshot = data_store.get_all_companies()
        company = next((c for c in companies_snapshot if c["id"] == company_id), None)
        if not company:
            events.append({"type": "done"})
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
                push(f"董事索引 {director_index} 超出範圍，改用自動選擇")
                director_index = None
        if target_director is None:
            push("分析董監事名單，自動選擇最大股法人代表…")
            target_director = gcis_client.pick_largest_legal_director(directors)
            if target_director is not None:
                director_index = directors.index(target_director)

        if not target_director:
            push("此公司董監事中無法人代表，且未指定董事，無關係可分析")
            data_store.update_company(company_id, {"relationship_graph": {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "director_index": None,
                "parent": None,
                "siblings": [],
                "note": "此公司董監事中無法人代表",
            }})
            events.append({"type": "done"})
            return

        company_index = _build_company_index(companies_snapshot)
        # Director represents a legal entity if either:
        #  (a) `representative_of` is set (natural person as legal-entity proxy), or
        #  (b) the director's own name looks like a company (法人股東直接任董事)
        is_legal = bool((target_director.get("representative_of") or "").strip()) \
            or _looks_like_company_name(target_director.get("name") or "")

        if is_legal:
            result = await _build_legal_entity_anchor(company, target_director, company_index, push)
        else:
            result = await _build_person_anchor(company, target_director, company_index, push)

        if result is not None:
            parent_node, siblings, note = result
            data_store.update_company(company_id, {"relationship_graph": {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "director_index": director_index,
                "parent": parent_node,
                "siblings": siblings,
                "note": note,
            }})
            push("關係資料補強完成")

        events.append({"type": "done"})
    except Exception as e:
        push(f"分析失敗:{e}")
        events.append({"type": "done"})
    finally:
        _rel_running.discard(company_id)


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


async def _enrich_company(company_id: str, api_key: str = "", provider: str = "anthropic") -> None:
    _running.add(company_id)
    events: list[dict] = []
    _progress[company_id] = events

    def push(msg: str):
        events.append({"type": "progress", "message": msg})

    def push_data(fields: dict):
        events.append({"type": "data", "fields": fields})

    try:
        company = data_store.get_company(company_id)
        if not company:
            events.append({"type": "done"})
            return

        name = company["name"]
        stored_tax_id = company.get("tax_id", "")
        push(f"正在查詢公司資料：{name}")

        try:
            # If we already know the tax_id (from user disambiguation), look up the
            # official full name first so Ronny search is unambiguous.
            lookup_name = name
            if stored_tax_id:
                official = await gcis_client.fetch_company_name_by_tax_id(stored_tax_id)
                if official:
                    lookup_name = official
            enrichment = await gcis_client.fetch_company_data(lookup_name)
            matched_name: str = enrichment.pop("matched_name", "")
            data_store.update_company(company_id, enrichment)
            directors_count = len(enrichment.get("directors", []))
            push_data({k: v for k, v in enrichment.items()})
            push(f"基本資料已更新（資本額、代表人、董監事 {directors_count} 人）")

            # Correct stored name to API-returned short name (strip legal suffix)
            if matched_name:
                short = matched_name
                for sfx in ("股份有限公司", "有限公司"):
                    if short.endswith(sfx):
                        short = short[:-len(sfx)]
                        break
                if short and short != name:
                    data_store.update_company(company_id, {"name": short})
                    push_data({"name": short})
                    push(f"公司名稱更新為：{short}")
        except Exception as e:
            push(f"資料查詢失敗：{e}，跳過繼續")

        push("正在生成公司簡介（約 30-60 秒）…")
        company = data_store.get_company(company_id)
        try:
            result = await report_generator.generate_summary(company, api_key=api_key, provider=provider)
            summary = result["summary"]
            blurb   = result["blurb"]
            data_store.update_company(company_id, {"summary": summary, "blurb": blurb})
            push_data({"summary": summary, "blurb": blurb})
            push("公司簡介已生成完成")
        except Exception as e:
            push(f"簡介生成失敗：{e}")

        try:
            from services.jk_nb_exporter import export_company_to_jk_nb
            export_company_to_jk_nb(data_store.get_company(company_id) or {})
        except Exception:
            log.exception("jk_nb export failed for company %s (non-fatal)", company_id)

        events.append({"type": "done"})
    finally:
        _running.discard(company_id)


async def _deep_enrich_company(company_id: str, api_key: str = "", provider: str = "anthropic") -> None:
    events: list[dict] = []
    _deep_progress[company_id] = events

    def push(msg: str):
        events.append({"type": "progress", "message": msg})

    def push_data(fields: dict):
        events.append({"type": "data", "fields": fields})

    try:
        company = data_store.get_company(company_id)
        if not company:
            events.append({"type": "done"})
            return

        push("正在深度搜尋媒體報導與新聞（約 60-120 秒）…")
        try:
            result = await report_generator.deep_enrich_summary(company, api_key=api_key, provider=provider)
            summary = result["summary"]
            blurb   = result["blurb"]
            data_store.update_company(company_id, {"summary": summary, "blurb": blurb})
            push_data({"summary": summary, "blurb": blurb})
            push("深度生成完成")
        except Exception as e:
            push(f"深度生成失敗：{e}")

        try:
            from services.jk_nb_exporter import export_company_to_jk_nb
            export_company_to_jk_nb(data_store.get_company(company_id) or {})
        except Exception:
            log.exception("jk_nb export failed for company %s (non-fatal)", company_id)

        events.append({"type": "done"})
    finally:
        if not events or events[-1].get("type") != "done":
            events.append({"type": "done"})
        _deep_running.discard(company_id)
