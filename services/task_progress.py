"""Shared SSE progress-stream helper.

A background task appends `{"type": ...}` events to `progress_map[key]`; this
generator polls that list and streams new events out as SSE `data: ...` lines
until a terminal event appears (or `max_ticks` is hit). A second caller for the
same `key` while it's already running just attaches to the same in-flight
stream instead of starting a duplicate task.

Used by routers/companies.py (six progress kinds: enrich/deep-enrich/summarize/
refresh-gcis/patents/build-relationship) and routers/industry_map.py.

Deliberately NOT used by services/daily_digest.py or routers/findbiz.py — both
have a genuinely different shape (daily_digest single-flights by polling a
persisted cache file rather than an in-memory event log; findbiz needs a
mid-stream external signal from the user, which fits asyncio.Queue better than
an append-only list). Forcing those into this helper would add branching
instead of removing duplication.
"""
import asyncio
import json


async def sse_progress_stream(
    key: str,
    progress_map: dict[str, list[dict]],
    running_set: set[str],
    start,
    *,
    max_ticks: int = 3600,
    interval: float = 0.5,
    terminal: tuple[str, ...] = ("done",),
    keepalive: bool = False,
):
    """把背景任務 append 到 progress_map[key] 的事件依序送出，直到出現 terminal
    事件或達 max_ticks。輸出格式固定為 SSE `data: ...`。
    start：一個無參 callable，負責啟動背景任務（通常是 lambda: asyncio.create_task(...)）；
    只有 key 尚未在 running_set 中時才會被呼叫一次——第二個呼叫者會安靜地接上
    同一條正在跑的串流，不會重複啟動背景任務。"""
    if key not in running_set:
        running_set.add(key)
        start()
    sent = 0
    try:
        for tick in range(max_ticks):
            events = progress_map.get(key, [])
            while sent < len(events):
                yield f"data: {json.dumps(events[sent], ensure_ascii=False)}\n\n"
                sent += 1
            if events and events[-1].get("type") in terminal:
                break
            # keepalive 只為防 proxy 閒置斷線，10 秒一次就夠——不必每個 tick（0.5s）
            # 都送，長任務掛 30–60 分鐘時可省 95% 的無意義訊息
            if keepalive and tick % 20 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(interval)
        yield 'data: {"type": "done"}\n\n'
    finally:
        progress_map.pop(key, None)
