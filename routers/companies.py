import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from services import claude_client, company_extractor, company_exporter, data_store, gcis_client, report_generator, patent_scraper
from services.ai_deps import ai_from_headers, ai_from_query

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/companies", tags=["companies"])

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_progress: dict[str, list[dict]] = {}
_running: set[str] = set()
_rel_progress: dict[str, list[dict]] = {}
_rel_running: set[str] = set()
_deep_progress: dict[str, list[dict]] = {}
_deep_running: set[str] = set()
_patent_progress: dict[str, list[dict]] = {}
_patent_running: set[str] = set()
_gcis_progress: dict[str, list[dict]] = {}
_gcis_running: set[str] = set()
_summarize_progress: dict[str, list[dict]] = {}
_summarize_running: set[str] = set()


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
    tax_id: str | None = None
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
    website: str | None = None


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
async def export_company(company_id: str, format: str = Query("docx", pattern="^(docx|pdf)$")):
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
        data = company_exporter.build_pdf(company, holders)
        return Response(
            content=data,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}.pdf"},
        )
    else:
        data = company_exporter.build_docx(company, holders)
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
            if req.enrich:
                enriching.append(company["id"])
                _running.add(company["id"])
                asyncio.create_task(_enrich_company(company["id"], **ai))
        else:
            if item.existing_id:
                updated = data_store.add_label_to_company(item.existing_id, item.label)
                if updated is not None:
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


async def _summarize_company(company_id: str, api_key: str = "", provider: str = "anthropic") -> None:
    """Summary-only enrichment: skips GCIS fetch, only runs AI summary generation."""
    events: list[dict] = []
    _summarize_progress[company_id] = events

    def push(msg: str):
        events.append({"type": "progress", "message": msg})

    def push_data(fields: dict):
        events.append({"type": "data", "fields": fields})

    try:
        company = data_store.get_company(company_id)
        if not company:
            events.append({"type": "done"})
            return

        push("正在生成公司簡介（約 3–7 分鐘）…")
        try:
            ctx = _gather_competitor_context(company_id, company.get("name", ""))
            if ctx["direct"]:
                push(f"偵測到 {len(ctx['direct'])} 家直接競業、{len(ctx['extended'])} 家延伸競業，將一併納入分析…")
            result = await report_generator.generate_summary(
                company, api_key=api_key, provider=provider, competitor_context=ctx or None
            )
            saved = _save_summary_result(company_id, result)
            push_data({"summary": saved["summary"], "blurb": saved["blurb"]})
            push("公司簡介已生成完成")
        except Exception as e:
            push(f"簡介生成失敗：{e}")

        events.append({"type": "done"})
    finally:
        _summarize_running.discard(company_id)


@router.get("/{company_id}/summarize")
async def summarize_stream(company_id: str, ai: dict = Depends(ai_from_query)):
    """SSE: regenerate AI summary only, without re-fetching GCIS data."""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    async def event_generator():
        if company_id not in _summarize_running:
            _summarize_running.add(company_id)
            asyncio.create_task(_summarize_company(company_id, **ai))
        sent = 0
        for _ in range(3600):
            events = _summarize_progress.get(company_id, [])
            while sent < len(events):
                yield f"data: {json.dumps(events[sent], ensure_ascii=False)}\n\n"
                sent += 1
            if events and events[-1].get("type") == "done":
                break
            await asyncio.sleep(0.5)
        yield 'data: {"type": "done"}\n\n'
        _summarize_progress.pop(company_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/{company_id}/find-website")
async def find_website(company_id: str, ai: dict = Depends(ai_from_query)):
    """Quick WebSearch to find the company's official website URL."""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    name = company.get("name", "")
    tax_id = company.get("tax_id", "")
    full = name if any(name.endswith(s) for s in ("股份有限公司", "有限公司")) else name + "股份有限公司"

    prompt = (
        f"請用 WebSearch 搜尋「{full}」（統編：{tax_id}）的官方網站。\n"
        f"只輸出最可能的官方網站 URL（含 https://），禁止任何其他說明文字。\n"
        f"若找不到官方網站，輸出空字串。\n"
        f"範例輸出：https://example.com"
    )
    try:
        result = await asyncio.to_thread(
            claude_client.ask,
            prompt, 60, ["WebSearch"],
            ai.get("api_key", ""), ai.get("provider", "anthropic"), 6,
        )
        url = result.strip().split("\n")[0].strip()
        if not url.startswith("http"):
            return {"website": ""}

        # Verify the URL is actually reachable before returning it
        try:
            async with httpx.AsyncClient(
                timeout=8.0, follow_redirects=True,
                headers={"User-Agent": _UA},
            ) as vc:
                resp = await vc.head(url)
                if resp.status_code >= 400:
                    # Some servers reject HEAD; try a GET as fallback
                    resp = await vc.get(url)
            if resp.status_code >= 400:
                log.info("find-website URL unreachable (%s) for %s: %s", resp.status_code, company_id, url)
                return {"website": ""}
        except Exception as verify_exc:
            log.info("find-website URL verify failed for %s (%s): %s", company_id, url, verify_exc)
            return {"website": ""}

        return {"website": url}
    except Exception as exc:
        log.warning("find-website failed for %s: %s", company_id, exc)
        return {"website": ""}


@router.get("/{company_id}/refresh-gcis")
async def refresh_gcis_stream(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    async def event_generator():
        if company_id not in _gcis_running:
            _gcis_running.add(company_id)
            asyncio.create_task(_refresh_gcis_only(company_id))
        sent = 0
        try:
            for _ in range(120):
                events = _gcis_progress.get(company_id, [])
                while sent < len(events):
                    yield f"data: {json.dumps(events[sent], ensure_ascii=False)}\n\n"
                    sent += 1
                if events and events[-1].get("type") == "done":
                    break
                await asyncio.sleep(0.5)
            yield 'data: {"type": "done"}\n\n'
        finally:
            _gcis_progress.pop(company_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _refresh_gcis_only(company_id: str) -> None:
    events: list[dict] = []
    _gcis_progress[company_id] = events

    try:
        company = data_store.get_company(company_id)
        if not company:
            events.append({"type": "done"})
            return

        name = company["name"]
        stored_tax_id = company.get("tax_id", "")
        events.append({"type": "progress", "message": f"正在重新拉取 GCIS 資料：{name}"})

        try:
            if stored_tax_id:
                enrichment = await gcis_client.fetch_company_data_by_tax_id(stored_tax_id)
            else:
                enrichment = await gcis_client.fetch_company_data(name)
            matched_name: str = enrichment.pop("matched_name", "")
            data_store.update_company(company_id, enrichment)
            directors_count = len(enrichment.get("directors", []))
            events.append({"type": "data", "fields": {k: v for k, v in enrichment.items()}})
            events.append({"type": "progress", "message": f"基本資料已更新（資本額、代表人、董監事 {directors_count} 人）"})

            if matched_name and matched_name != name:
                data_store.update_company(company_id, {"name": matched_name})
                events.append({"type": "data", "fields": {"name": matched_name}})
        except Exception as e:
            events.append({"type": "progress", "message": f"資料查詢失敗：{e}"})

        events.append({"type": "done"})
    finally:
        _gcis_running.discard(company_id)


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


def _gather_competitor_context(company_id: str, company_name: str) -> dict:
    """
    Return two layers of competitor context for prompt injection:
      direct   – companies in DB that explicitly list this company as their competitor
      extended – those companies' own DB-linked competitors (one hop), for AI reference only
    """
    name_key = _short(company_name)
    all_cos = data_store.get_all_companies()
    by_id = {c["id"]: c for c in all_cos}

    # Layer 1: direct (companies that list this company as competitor)
    direct: list[dict] = []
    seen_direct: set[str] = set()
    for other in all_cos:
        if other["id"] == company_id:
            continue
        for comp in (other.get("competitors") or []):
            if comp.get("company_id") == company_id or _short(comp.get("name", "")) == name_key:
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


def _resolve_competitor_ids(competitors: list[dict]) -> list[dict]:
    """Fill in company_id for competitors that are already in the DB."""
    all_cos = data_store.get_all_companies()
    # Index by both stored name and short name so full/short mismatches resolve correctly
    name_to_id: dict[str, str] = {}
    for c in all_cos:
        name_to_id[c["name"]] = c["id"]
        name_to_id[_short(c["name"])] = c["id"]
    for comp in competitors:
        name = comp.get("name", "")
        comp["company_id"] = name_to_id.get(name) or name_to_id.get(_short(name)) or None
    return competitors


def _backlink_competitor(new_id: str, new_name: str) -> None:
    """When a new company is added, update other companies' competitors[].company_id."""
    new_key = _short(new_name)
    for co in data_store.get_all_companies():
        if co["id"] == new_id:
            continue
        comps = co.get("competitors")
        if not comps:
            continue
        updated = False
        for comp in comps:
            if comp.get("company_id") is None and _short(comp.get("name", "")) == new_key:
                comp["company_id"] = new_id
                updated = True
        if updated:
            data_store.update_company(co["id"], {"competitors": comps})


def _save_summary_result(company_id: str, result: dict, extra: dict | None = None) -> dict:
    """Persist summary/blurb/competitors from a generation result. `extra` folds
    additional fields into the same write (avoids a second full-file rewrite).
    Returns the fields saved."""
    fields: dict = {
        "summary": result.get("summary", ""),
        "blurb":   result.get("blurb", ""),
        # A full public-data regen replaces the whole summary, so any
        # previously-applied 簡報 section markers no longer apply.
        "materials_applied_headings": [],
    }
    if "competitors" in result:
        fields["competitors"] = _resolve_competitor_ids(result["competitors"])
    if extra:
        fields.update(extra)
    data_store.update_company(company_id, fields)
    return fields


class AddCompetitorRequest(BaseModel):
    name: str
    competition_type: str


_COMPETITION_TYPES = {"正面競業", "替代路徑", "側翼潛入", "垂直整合"}


def _insert_competitor_row(summary: str, row: str) -> str:
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
            if stored_tax_id:
                enrichment = await gcis_client.fetch_company_data_by_tax_id(stored_tax_id)
            else:
                enrichment = await gcis_client.fetch_company_data(name)
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

        push("正在生成公司簡介（約 3–7 分鐘）…")
        company = data_store.get_company(company_id)
        if not company:
            events.append({"type": "done"})
            return
        try:
            ctx = _gather_competitor_context(company_id, company.get("name", ""))
            if ctx["direct"]:
                push(f"偵測到 {len(ctx['direct'])} 家直接競業、{len(ctx['extended'])} 家延伸競業，將一併納入分析…")
            result = await report_generator.generate_summary(
                company, api_key=api_key, provider=provider, competitor_context=ctx or None
            )
            saved = _save_summary_result(company_id, result)
            push_data({"summary": saved["summary"], "blurb": saved["blurb"]})
            push("公司簡介已生成完成")
            # Reverse link: if this company appears in other companies' competitor lists, fill company_id
            _backlink_competitor(company_id, company["name"])
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

        push("正在深度搜尋媒體報導與新聞（約 4–8 分鐘）…")
        try:
            ctx = _gather_competitor_context(company_id, company.get("name", ""))
            if ctx["direct"]:
                push(f"偵測到 {len(ctx['direct'])} 家直接競業、{len(ctx['extended'])} 家延伸競業，將一併納入分析…")
            result = await report_generator.deep_enrich_summary(
                company, api_key=api_key, provider=provider, competitor_context=ctx or None
            )
            # Mark that a deep enrich has completed, so the UI can warn before
            # re-running it (distinct from last_updated, which any update touches).
            deep_at = datetime.now(timezone.utc).isoformat()
            saved = _save_summary_result(company_id, result, extra={"deep_enriched_at": deep_at})
            push_data({"summary": saved["summary"], "blurb": saved["blurb"], "deep_enriched_at": deep_at})
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
