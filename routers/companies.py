import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services import company_extractor, data_store, gcis_client, report_generator
from services.ai_deps import ai_from_headers, ai_from_query

router = APIRouter(prefix="/api/companies", tags=["companies"])

_progress: dict[str, list[dict]] = {}
_running: set[str] = set()


class ConfirmItem(BaseModel):
    name: str
    label: str
    industry: str | None = None
    is_new: bool
    existing_id: str | None = None


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


@router.get("/{company_id}")
def get_company(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.post("/confirm")
async def confirm_companies(req: ConfirmRequest, ai: dict = Depends(ai_from_headers)):
    saved_ids: list[str] = []
    enriching: list[str] = []

    for item in req.companies:
        data_store.add_label(item.label)

        if item.is_new:
            company = data_store.create_company(item.name, item.label, item.industry)
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
        push(f"正在查詢公司資料：{name}")

        try:
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

        events.append({"type": "done"})
    finally:
        _running.discard(company_id)
