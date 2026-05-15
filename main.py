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

from routers import companies, config, upload, call_memo, industries, findbiz

log = logging.getLogger(__name__)
TAIWAN_TZ = timezone(timedelta(hours=8))


async def _daily_digest_scheduler() -> None:
    """Trigger digest refresh for all industries every day at 08:00 Taiwan time."""
    while True:
        now = datetime.now(TAIWAN_TZ)
        today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
        next_8am = today_8am if now < today_8am else today_8am + timedelta(days=1)
        wait = (next_8am - now).total_seconds()
        log.info("Daily digest scheduler: next run in %.0f s", wait)
        await asyncio.sleep(wait)
        from services.daily_digest import refresh_all_digests
        await refresh_all_digests()


async def _daily_trends_scheduler() -> None:
    """Trigger trend refresh for all industries every day at 08:05 Taiwan time (5 min after digest)."""
    while True:
        now = datetime.now(TAIWAN_TZ)
        today_8_05 = now.replace(hour=8, minute=5, second=0, microsecond=0)
        next_8_05 = today_8_05 if now < today_8_05 else today_8_05 + timedelta(days=1)
        wait = (next_8_05 - now).total_seconds()
        log.info("Daily trends scheduler: next run in %.0f s", wait)
        await asyncio.sleep(wait)
        from services.daily_digest import refresh_all_trends
        await refresh_all_trends()


@asynccontextmanager
async def lifespan(app: FastAPI):
    t1 = asyncio.create_task(_daily_digest_scheduler())
    t2 = asyncio.create_task(_daily_trends_scheduler())
    yield
    for t in (t1, t2):
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
app.include_router(config.router)
app.include_router(call_memo.router)
app.include_router(industries.router)
app.include_router(findbiz.router)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"), headers=_NO_CACHE)


@app.get("/health")
def health():
    return {"status": "ok"}
