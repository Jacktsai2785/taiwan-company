"""News blacklist endpoints — dismiss articles and trigger AI rule analysis."""
import asyncio
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from services import blacklist as bl_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/news", tags=["news-blacklist"])


class DismissRequest(BaseModel):
    url: str
    title: str
    source: str = ""


@router.post("/dismiss")
async def dismiss_article(body: DismissRequest):
    """Mark an article as unwanted. Triggers AI rule analysis every 5 dismissals."""
    state = bl_svc.dismiss(body.url, body.title, body.source)
    count = len(state["dismissed"])

    if count % 5 == 0:
        asyncio.create_task(bl_svc.analyze_with_ai())

    return {
        "dismissed_count": count,
        "rules": state["rules"],
    }


@router.get("/blacklist")
async def get_blacklist():
    """View current blacklist state and generated rules."""
    return bl_svc.load_all()


@router.post("/analyze")
async def trigger_analysis():
    """Manually trigger AI analysis of dismissed articles to update filter rules."""
    return await bl_svc.analyze_with_ai()
