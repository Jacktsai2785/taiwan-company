#!/usr/bin/env python3
"""
Batch migrate all companies' summaries to the new 5-column competitor format.

Progress is tracked in data/migrate_progress.json so the script can be run
repeatedly and resume from where it left off.

Designed to run via systemd timer every hour:
  - If still inside a session-limit wait window → exit immediately
  - Otherwise → process companies one by one until done or limit hit
"""
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import data_store, report_generator
from routers.companies import _gather_competitor_context, _save_summary_result

LOG_FILE = REPO_ROOT / "logs" / "migrate_summaries.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("migrate_summaries")

PROGRESS_FILE = REPO_ROOT / "data" / "migrate_progress.json"
SESSION_LIMIT_WAIT_HOURS = 5.5
# Keywords that indicate a Claude session / rate limit (cast wide net)
_LIMIT_KEYWORDS = [
    "usage limit", "rate limit", "rate_limit", "session limit",
    "too many requests", "429", "capacity", "overloaded",
    "quota", "at capacity", "please try again",
]
# If this many consecutive non-limit errors happen, treat as limit to be safe
_CONSECUTIVE_ERROR_THRESHOLD = 3


# ── helpers ───────────────────────────────────────────────────────────────────

def _is_old_format(summary: str) -> bool:
    """Return True if the summary still uses the legacy 4-column competitor table."""
    in_section = False
    for line in summary.split("\n"):
        s = line.strip()
        if re.match(r"^##\s+競業分析", s):
            in_section = True
            continue
        if in_section and re.match(r"^##\s+", s):
            break
        if not in_section:
            continue
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s[1:-1].split("|")]
            if cells[0] == "公司名稱" or all(re.match(r"^-*$", c) for c in cells):
                continue
            if "（本案）" in cells[0]:
                continue
            return len(cells) < 5
    return False


def _is_limit_error(err: str) -> bool:
    low = err.lower()
    return any(kw in low for kw in _LIMIT_KEYWORDS)


def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "pending": [],
        "done": [],
        "failed": [],
        "limit_hit_at": None,
        "started_at": None,
    }


def _save_progress(p: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    p = _load_progress()

    # ── check wait window ────────────────────────────────────────────────────
    if p.get("limit_hit_at"):
        hit_time = datetime.fromisoformat(p["limit_hit_at"])
        elapsed_h = (datetime.now(timezone.utc) - hit_time).total_seconds() / 3600
        if elapsed_h < SESSION_LIMIT_WAIT_HOURS:
            remaining = SESSION_LIMIT_WAIT_HOURS - elapsed_h
            log.info("Session limit 等待中，還需 %.1f 小時。本次結束。", remaining)
            return
        log.info("等待時間已過（%.1f h），繼續遷移。", elapsed_h)
        p["limit_hit_at"] = None

    # ── build / refresh pending list ─────────────────────────────────────────
    if not p["pending"]:
        all_cos = data_store.get_all_companies()
        done_set = set(p.get("done", []))
        failed_set = set(p.get("failed", []))
        p["pending"] = [
            c["id"] for c in all_cos
            if c["id"] not in done_set
            and c["id"] not in failed_set
            and _is_old_format(c.get("summary") or "")
        ]
        if not p.get("started_at"):
            p["started_at"] = datetime.now(timezone.utc).isoformat()
        log.info(
            "待遷移：%d 間　已完成：%d 間　失敗：%d 間",
            len(p["pending"]), len(p["done"]), len(p["failed"]),
        )
        _save_progress(p)

    if not p["pending"]:
        log.info("全部遷移完畢！完成：%d 間，失敗：%d 間", len(p["done"]), len(p["failed"]))
        return

    log.info("開始處理，本輪剩餘 %d 間", len(p["pending"]))
    consecutive_errors = 0

    while p["pending"]:
        cid = p["pending"][0]
        company = data_store.get_company(cid)
        if not company:
            log.warning("找不到公司 %s，跳過", cid)
            p["pending"].pop(0)
            p["failed"].append(cid)
            _save_progress(p)
            continue

        name = company.get("name", cid)
        log.info("正在生成：%s（剩 %d 間）", name, len(p["pending"]))

        try:
            ctx = _gather_competitor_context(cid, name)
            result = await report_generator.generate_summary(
                company, competitor_context=ctx or None
            )
            _save_summary_result(cid, result)

            p["pending"].pop(0)
            p["done"].append(cid)
            _save_progress(p)
            consecutive_errors = 0
            log.info("完成：%s", name)

        except Exception as exc:
            err_str = str(exc)
            log.warning("錯誤：%s — %s", name, err_str[:300])
            consecutive_errors += 1

            if _is_limit_error(err_str) or consecutive_errors >= _CONSECUTIVE_ERROR_THRESHOLD:
                log.info(
                    "偵測到 session limit（連續錯誤 %d 次），儲存進度並結束。"
                    "下次排程將在 %.1f 小時後自動繼續。",
                    consecutive_errors, SESSION_LIMIT_WAIT_HOURS,
                )
                p["limit_hit_at"] = datetime.now(timezone.utc).isoformat()
                _save_progress(p)
                sys.exit(0)

            # Single non-limit error: skip this company and continue
            p["pending"].pop(0)
            p["failed"].append(cid)
            _save_progress(p)
            log.warning("跳過：%s", name)
            consecutive_errors = 0  # reset after skipping

    log.info(
        "本輪結束。完成：%d 間，失敗：%d 間，剩餘：%d 間",
        len(p["done"]), len(p["failed"]), len(p["pending"]),
    )


if __name__ == "__main__":
    asyncio.run(main())
