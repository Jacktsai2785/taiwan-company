from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from services import data_store, company_extractor
from services.ai_deps import ai_from_headers

router = APIRouter(prefix="/api/config", tags=["config"])


class IndustryAdd(BaseModel):
    name: str


class IndustryRename(BaseModel):
    old_name: str
    new_name: str


class IndustrySuggest(BaseModel):
    name: str


@router.get("")
def get_config():
    return data_store.get_config()


@router.get("/industries")
def get_industries():
    return data_store.get_industries()


@router.get("/groups")
def get_groups():
    """Return {industry: [group, ...]} derived from company data."""
    companies = data_store.get_all_companies()
    groups: dict[str, list[str]] = {}
    for c in companies:
        ind = c.get("industry") or ""
        grp = c.get("group") or ""
        if ind not in groups:
            groups[ind] = []
        if grp and grp not in groups[ind]:
            groups[ind].append(grp)
    return groups


@router.post("/industries/suggest")
async def suggest_industry_match(req: IndustrySuggest, ai: dict = Depends(ai_from_headers)):
    """Ask Claude which existing companies fit the new industry name."""
    companies = data_store.get_all_companies()
    matched_ids = await company_extractor.suggest_companies_for_industry(req.name.strip(), companies, **ai)
    return {"matched_ids": matched_ids}


@router.post("/industries")
async def add_industry(
    req: IndustryAdd,
    background_tasks: BackgroundTasks,
    ai: dict = Depends(ai_from_headers),
):
    name = req.name.strip()
    is_new = name not in data_store.get_industries()
    industries = data_store.add_industry(name)
    if is_new:
        from services.daily_digest import generate_industry_keywords
        background_tasks.add_task(
            generate_industry_keywords, name, ai["api_key"], ai["provider"]
        )
    return {"industries": industries}


@router.put("/industries")
def rename_industry(req: IndustryRename):
    if not req.old_name.strip() or not req.new_name.strip():
        raise HTTPException(status_code=422, detail="名稱不可為空")
    industries = data_store.rename_industry(req.old_name.strip(), req.new_name.strip())
    return {"industries": industries}


@router.delete("/industries/{name}")
def delete_industry(name: str):
    industries = data_store.delete_industry(name)
    return {"industries": industries}
