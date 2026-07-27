"""Industry map endpoints: read cached / generate (SSE) / delete / subdivide."""
import asyncio
from services.task_progress import spawn_background as _spawn
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services import data_store, industry_map
from services.ai_deps import ai_from_query
from services.task_progress import sse_progress_stream

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/industry-map", tags=["industry-map"])

_progress: dict[str, list[dict]] = {}
_running: set[str] = set()

# subdivide/propose 的 SSE 進度（key 前綴 "sub:" 以免和 generate 的 key 撞）
_sub_progress: dict[str, list[dict]] = {}
_sub_running: set[str] = set()

_REPO_DIR = Path(__file__).parent.parent


def _run_backup() -> None:
    """細分寫入 companies.json 前先備份一次（best-effort，失敗不擋——尚有每日 timer + 原子寫）。"""
    try:
        subprocess.run(
            ["bash", str(_REPO_DIR / "scripts" / "backup_data.sh")],
            timeout=60, capture_output=True,
        )
    except Exception:
        log.warning("subdivide: 備份腳本執行失敗（繼續進行）", exc_info=True)


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
    ai: dict = Depends(ai_from_query),
):
    """SSE: 觸發 AI 生成，串流進度訊息，最後送 done event 帶完整結果。
    有子產業 → 父模式總覽；無子產業 → 葉模式完整歸位（見 services.industry_map）。"""
    # 佔用守衛 + 啟動任務都在 endpoint body 同步做：檢查與 add 之間沒有 await → 原子，
    # 兩個近同時的請求不會都通過（若放進 sse_progress_stream，守衛只在串流第一個 tick
    # 才設，窗口變寬 → 可能雙開 → 對 industry_maps.json 未上鎖讀寫 lost-update）。
    if industry in _running:
        raise HTTPException(status_code=409, detail="該產業正在生成中，請稍候再試")
    _running.add(industry)

    _progress[industry] = []
    # 持有清單「參照」而非每次都 _progress[industry]：SSE 串流在 client 斷線時會 pop 掉
    # 這個 key，若背景任務仍 append 到 _progress[industry] 會 KeyError、任務崩潰、永遠送
    # 不出終端事件 → 前端按鈕永遠卡在「生成中」。改寫進 captured list 就不受 pop 影響。
    events = _progress[industry]

    def on_progress(msg: str) -> None:
        events.append({"type": "progress", "message": msg})

    async def run() -> None:
        try:
            result = await industry_map.generate(
                industry,
                engine=ai["engine"],
                progress_cb=on_progress,
            )
            events.append({"type": "done", "data": result})
        except Exception as e:
            log.exception("產業地圖生成失敗：%s", industry)
            events.append({"type": "error", "message": str(e)})
        finally:
            _running.discard(industry)

    _spawn(run())
    return StreamingResponse(
        # start 傳 no-op：已在上面同步佔用 _running 並啟動任務，sse_progress_stream 只負責串流
        sse_progress_stream(
            industry, _progress, _running, lambda: None,
            max_ticks=7200, interval=0.5, terminal=("done", "error"), keepalive=True,
        ),
        media_type="text/event-stream",
    )


# ── Subdivision (Phase 2)：把產業細分成子產業 ────────────────────────────────────

@router.get("/{industry}/subdivide/propose")
async def propose_subdivision(industry: str, ai: dict = Depends(ai_from_query)):
    """SSE：用 AI 把「還沒分組」的公司（父產業的未分類堆）分成子產業草稿。不寫入。
    最後 done event 帶 {industry, groups}。葉圖的「收成子產業」不走這裡——前端直接用
    現成 sections 算 groups，省一次 AI call。"""
    key = f"sub:{industry}"
    # 同 generate：守衛 + 啟動同步做在 endpoint body（原子、關掉並發窗口）
    if key in _sub_running:
        raise HTTPException(status_code=409, detail="該產業正在細分中，請稍候再試")
    _sub_running.add(key)

    _sub_progress[key] = []
    events = _sub_progress[key]  # captured ref（同 generate：避免 pop 後 KeyError，見上）

    def on_progress(msg: str) -> None:
        events.append({"type": "progress", "message": msg})

    async def run() -> None:
        try:
            result = await industry_map.propose_subdivision(
                industry, engine=ai["engine"], emit=on_progress,
            )
            events.append({"type": "done", "data": result})
        except Exception as e:
            log.exception("細分提議失敗：%s", industry)
            events.append({"type": "error", "message": str(e)})
        finally:
            _sub_running.discard(key)

    _spawn(run())
    return StreamingResponse(
        # start no-op：已同步佔用 + 啟動
        sse_progress_stream(
            key, _sub_progress, _sub_running, lambda: None,
            max_ticks=7200, interval=0.5, terminal=("done", "error"), keepalive=True,
        ),
        media_type="text/event-stream",
    )


class SubdivideRequest(BaseModel):
    groups: list[dict]  # [{"name": 子產業名, "company_ids": [...]}]


@router.post("/{industry}/subdivide")
def subdivide_industry(industry: str, req: SubdivideRequest):
    """把使用者確認過的分組收成子產業：先備份 → retag companies + 建子產業/掛樹 →
    讓 parent 的舊快取地圖失效（下次打開會以父模式重生成總覽）。"""
    groups = [
        {"name": (g.get("name") or "").strip(), "company_ids": g.get("company_ids") or []}
        for g in req.groups
        if (g.get("name") or "").strip() and (g.get("company_ids") or [])
    ]
    if not groups:
        raise HTTPException(status_code=422, detail="沒有可收成子產業的分組")

    _run_backup()
    result = data_store.apply_subdivision(industry, groups)
    industry_map.delete_map(industry)  # 舊葉圖已過時，改父模式重生成

    return {
        "industry": industry,
        "tree": data_store.get_industry_tree(),
        "industries": data_store.get_industries(),
        **result,
    }


@router.post("/{industry}/merge")
def merge_subdivision(industry: str):
    """取消細分（apply_subdivision 的逆操作）：把此產業底下所有子產業（含各層後代）合併
    回本產業，公司重新掛回、子產業移除。之後本產業回到葉模式 → 重新生成會是單張完整地圖
    （含競業候選）。先備份，並刪掉受影響的快取地圖。"""
    tree = data_store.get_industry_tree()
    descendants: list[str] = []
    stack = list(tree.get(industry, []))
    while stack:
        node = stack.pop()
        descendants.append(node)
        stack.extend(tree.get(node, []))
    if not descendants:
        raise HTTPException(status_code=422, detail="此產業沒有子產業可合併")

    _run_backup()
    result = data_store.merge_children_into(industry, descendants)
    industry_map.delete_map(industry)          # 父模式總覽已過時 → 回葉模式重生成
    for d in descendants:
        industry_map.delete_map(d)

    return {
        "industry": industry,
        "tree": data_store.get_industry_tree(),
        "industries": data_store.get_industries(),
        "merged": descendants,
        **result,
    }
