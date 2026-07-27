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
import contextlib
import json

# 保存 fire-and-forget 任務的強參照，避免 event loop 只持弱參照時被 GC 中途取消。
# 泛用工具，不是 enrichment 專屬——原本放在 routers/enrichment.py，被
# companies.py/industry_map.py/materials.py/findbiz.py 直接 import 其私有函式，
# router 之間因此互相耦合成 application service。搬來這個共用 service 層模組，
# 四個 router 各自 import 同一份公開函式，不再互相依賴對方的私有實作。
_BG_TASKS: set[asyncio.Task] = set()


def spawn_background(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


class _Emitter:
    """交給背景 worker 用來 append 事件，取代各 worker 自己手刻的
    push/push_data/push_error closure。"""

    __slots__ = ("_events",)

    def __init__(self, events: list[dict]):
        self._events = events

    def progress(self, message: str) -> None:
        self._events.append({"type": "progress", "message": message})

    def data(self, fields: dict) -> None:
        self._events.append({"type": "data", "fields": fields})

    def error(self, message: str, code: str = "") -> None:
        self._events.append({"type": "error", "message": message, "code": code})

    def done(self, ok: bool = True, **extra) -> None:
        self._events.append({"type": "done", "ok": ok, **extra})


class ProgressChannel:
    """一組 (progress dict, running set) + 背景 worker 生命週期管理，取代
    routers/enrichment.py（4 組）與 routers/companies.py（2 組：build-relationship
    的 rel、patents 的 patent）各自手刻的 events/push/push_data/push_error +
    try/finally discard 樣板。

    用法：
        _enrich_channel = ProgressChannel()

        async def _enrich_company(company_id, ...):
            with _enrich_channel.session(company_id) as ev:
                ev.progress("...")
                ev.data({...})
                ev.error("...", code="...")   # 不是終止事件，之後仍要呼叫 ev.done()
                ev.done(ok=True)

        @router.get(...)
        def stream(...):
            return StreamingResponse(_enrich_channel.stream(company_id, start_fn), ...)

    `running` 是否在 key 進入時被加入，由呼叫端決定（單筆串流靠 stream()/
    sse_progress_stream 的 start() 閘門；批次端點自己先 add 再 spawn）——
    session() 只負責「離開時 discard」，不重複管這件事，維持跟現有兩種
    啟動路徑相容。"""

    def __init__(self):
        self.progress: dict[str, list[dict]] = {}
        self.running: set[str] = set()

    def stream(self, key: str, start, **kwargs):
        return sse_progress_stream(key, self.progress, self.running, start, **kwargs)

    @contextlib.contextmanager
    def session(self, key: str):
        events: list[dict] = []
        self.progress[key] = events
        try:
            yield _Emitter(events)
        finally:
            # worker 若意外中止（未捕捉的例外、提早 return 忘記呼叫 ev.done()），
            # 補一個 done(ok=False)，避免前端卡到 sse_progress_stream 的
            # max_ticks 逾時才假裝成功完成。
            if not events or events[-1].get("type") not in ("done", "error"):
                events.append({"type": "done", "ok": False})
            self.running.discard(key)


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
        # 只有事件流已達 terminal 才清除；若使用者中途斷線（GeneratorExit）而任務未完成，
        # 保留 progress_map[key]，讓重連的新串流能從頭接回同一份事件列表（背景任務仍在 append
        # 同一個 list 物件），而不是讀到空 progress → 轉圈後假 done、看不到任何進度。
        evs = progress_map.get(key)
        if not evs or evs[-1].get("type") in terminal:
            progress_map.pop(key, None)
