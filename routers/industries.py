from fastapi import APIRouter, Depends, Query

from services.ai_deps import ai_from_headers
from services.daily_digest import get_digest, get_trends

router = APIRouter(prefix="/api/industries", tags=["industries"])


@router.get("/{industry}/daily")
async def get_industry_daily(
    industry: str,
    refresh: bool = Query(default=False),
    ai: dict = Depends(ai_from_headers),
):
    """Return the daily news digest for the given industry.

    Cached per-industry per-day; pass ?refresh=true to force regeneration.
    """
    return await get_digest(
        industry,
        api_key=ai["api_key"],
        provider=ai["provider"],
        force_refresh=refresh,
    )


@router.get("/{industry}/trends")
async def get_industry_trends(
    industry: str,
    refresh: bool = Query(default=False),
    ai: dict = Depends(ai_from_headers),
):
    """Return the quarterly trend analysis for the given industry.

    Cached until the next weekly Monday refresh; pass ?refresh=true to force regeneration.
    """
    return await get_trends(
        industry,
        api_key=ai["api_key"],
        provider=ai["provider"],
        force_refresh=refresh,
    )
