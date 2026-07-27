import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Optional: load .env for CLAUDE_CLI_PATH override
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from routers import companies, competitors, enrichment, config, upload, call_memo, industries, findbiz, news_blacklist, industry_map, materials

log = logging.getLogger(__name__)
TAIWAN_TZ = timezone(timedelta(hours=8))


async def _daily_scheduler() -> None:
    """每天 08:00 台灣時間依序跑 digests → trends（不是兩個各自獨立、只靠固定
    時間差錯開的 task——trends 讀的是累積的歷史快取，跟正在寫入當天 digest 的
    檔案交錯執行沒有幫助，依序執行更乾淨）。

    refresh_all_digests/refresh_all_trends 內部仍會逐產業 try/except（單一產業
    失敗不影響其他產業），並回傳失敗清單；這裡只對失敗的產業做一次 1 小時後的
    有界重試，仍失敗才等到隔天——不再是「log 說 1 小時後重試、實際上因為例外
    早被內層吞掉而要等到隔天」的落差。"""
    from services.daily_digest import refresh_all_digests, refresh_all_trends
    while True:
        try:
            now = datetime.now(TAIWAN_TZ)
            today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
            next_8am = today_8am if now < today_8am else today_8am + timedelta(days=1)
            wait = (next_8am - now).total_seconds()
            log.info("Daily scheduler: next run in %.0f s", wait)
            await asyncio.sleep(wait)

            failed_digests = await refresh_all_digests()
            failed_trends = await refresh_all_trends()

            if failed_digests or failed_trends:
                log.warning(
                    "Daily scheduler: digest failed for %s, trends failed for %s — retrying in 1h",
                    failed_digests, failed_trends,
                )
                await asyncio.sleep(3600)
                still_failed_digests = await refresh_all_digests(failed_digests) if failed_digests else []
                still_failed_trends = await refresh_all_trends(failed_trends) if failed_trends else []
                if still_failed_digests or still_failed_trends:
                    log.warning(
                        "Daily scheduler: retry still failing for digests=%s trends=%s — giving up until tomorrow 08:00",
                        still_failed_digests, still_failed_trends,
                    )
        except asyncio.CancelledError:
            raise  # 正常關機路徑，讓它往上傳遞
        except Exception:
            # 排程本身（非個別產業）出錯才會走到這裡；個別產業失敗已經在
            # refresh_all_* 內被吞掉並回傳清單，不會讓整條 task 死掉。
            log.exception("Daily scheduler iteration failed unexpectedly; retrying in 1h")
            await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 啟動對帳：修補多段寫中斷留下的殭屍產業標籤（非破壞，把公司引用到但 config 沒有的補回）
    try:
        from services.data_store import reconcile_industries
        r = reconcile_industries()
        if r.get("readded_industries"):
            log.info("啟動對帳：補回 %d 個殭屍產業標籤到 config", len(r["readded_industries"]))
    except Exception:
        log.exception("啟動產業對帳失敗（非致命）")
    t1 = asyncio.create_task(_daily_scheduler())
    yield
    for t in (t1,):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(title="台灣產業商情平台", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload.router)
app.include_router(companies.router)
app.include_router(competitors.router)
app.include_router(enrichment.router)
app.include_router(config.router)
app.include_router(call_memo.router)
app.include_router(industries.router)
app.include_router(findbiz.router)
app.include_router(news_blacklist.router)
app.include_router(industry_map.router)
app.include_router(materials.router)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Uploaded company materials (簡報/介紹/照片) — served so the user can click to view originals
UPLOADS_DIR = Path(__file__).parent / "data" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"), headers=_NO_CACHE)


@app.get("/health")
def health():
    return {"status": "ok"}
