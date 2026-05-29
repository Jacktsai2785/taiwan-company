"""Industry map endpoints: read cached / generate (SSE) / delete."""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from services import industry_map
from services.ai_deps import ai_from_query

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/industry-map", tags=["industry-map"])

_progress: dict[str, list[dict]] = {}
_running: set[str] = set()


@router.get("/{industry}")
def get_industry_map(industry: str):
    data = industry_map.load_map(industry)
    if not data:
        raise HTTPException(status_code=404, detail="尚未生成")
    return data


@router.delete("/{industry}")
def delete_industry_map(industry: str):
    ok = industry_map.delete_map(industry)
    if not ok:
        raise HTTPException(status_code=404, detail="不存在")
    return {"deleted": industry}


@router.get("/{industry}/generate")
async def generate_industry_map(
    industry: str,
    breadth: str = Query(default="medium", pattern="^(narrow|medium|broad)$"),
    ai: dict = Depends(ai_from_query),
):
    """SSE: 觸發 AI 生成，串流進度訊息，最後送 done event 帶完整結果。"""
    if industry in _running:
        raise HTTPException(status_code=409, detail="該產業正在生成中，請稍候再試")

    _running.add(industry)
    _progress[industry] = []

    def on_progress(msg: str) -> None:
        _progress[industry].append({"type": "progress", "message": msg})

    async def run() -> None:
        try:
            result = await industry_map.generate(
                industry,
                breadth=breadth,
                api_key=ai["api_key"],
                provider=ai["provider"],
                progress_cb=on_progress,
            )
            _progress[industry].append({"type": "done", "data": result})
        except Exception as e:
            log.exception("產業地圖生成失敗：%s", industry)
            _progress[industry].append({"type": "error", "message": str(e)})
        finally:
            _running.discard(industry)

    asyncio.create_task(run())

    async def event_stream():
        sent = 0
        for _ in range(7200):  # 上限 60 分鐘
            events = _progress.get(industry, [])
            while sent < len(events):
                yield f"data: {json.dumps(events[sent], ensure_ascii=False)}\n\n"
                sent += 1
            if events and events[-1].get("type") in ("done", "error"):
                break
            await asyncio.sleep(0.5)
        _progress.pop(industry, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
