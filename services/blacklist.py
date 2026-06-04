"""User-driven article blacklist with AI-assisted rule generation."""
import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

BLACKLIST_PATH = Path(__file__).parent.parent / "data" / "blacklist.json"

_EMPTY: dict = {
    "dismissed": [],
    "rules": {
        "blocked_urls": [],
        "blocked_domains": [],
        "blocked_sources": [],
        "blocked_title_patterns": [],
        "last_analyzed_at": None,
        "ai_summary": None,
    },
}


def _load() -> dict:
    if BLACKLIST_PATH.exists():
        try:
            with open(BLACKLIST_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    import copy
    return copy.deepcopy(_EMPTY)


def _save(bl: dict) -> None:
    BLACKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BLACKLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(bl, f, ensure_ascii=False, indent=2)


def _extract_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host.removeprefix("www.")
    except Exception:
        return ""


def _update_heuristic_rules(bl: dict) -> None:
    dismissed = bl["dismissed"]
    rules = bl["rules"]

    domain_counts = Counter(d["domain"] for d in dismissed if d.get("domain"))
    rules["blocked_domains"] = [d for d, n in domain_counts.items() if n >= 2]

    source_counts = Counter(d["source"] for d in dismissed if d.get("source"))
    rules["blocked_sources"] = [s for s, n in source_counts.items() if n >= 2]

    rules["blocked_urls"] = [d["url"] for d in dismissed if d.get("url")]


def dismiss(url: str, title: str, source: str) -> dict:
    bl = _load()

    if any(d["url"] == url for d in bl["dismissed"]):
        return bl

    bl["dismissed"].append({
        "url": url,
        "title": title,
        "source": source,
        "domain": _extract_domain(url),
        "dismissed_at": datetime.now(timezone.utc).isoformat(),
    })

    _update_heuristic_rules(bl)
    _save(bl)
    log.info("Article dismissed: %s (total: %d)", title[:40], len(bl["dismissed"]))
    return bl


def get_dynamic_filters() -> tuple[set, set, set, tuple]:
    """Return (urls, domains, sources, title_patterns) as filter sets."""
    bl = _load()
    rules = bl.get("rules", {})
    return (
        set(rules.get("blocked_urls", [])),
        set(rules.get("blocked_domains", [])),
        set(rules.get("blocked_sources", [])),
        tuple(rules.get("blocked_title_patterns", [])),
    )


def load_all() -> dict:
    return _load()


async def analyze_with_ai() -> dict:
    """Use the local AI engine to extract title-pattern rules from dismissed articles."""
    from . import claude_client

    bl = _load()
    dismissed = bl.get("dismissed", [])
    if not dismissed:
        return {"skipped": True, "reason": "no dismissed articles yet"}

    lines = "\n".join(
        f"{i+1}. 標題：{d['title']} | 來源：{d['source']} | 網域：{d['domain']}"
        for i, d in enumerate(dismissed[-30:])
    )

    prompt = f"""你是一個台灣產業商情平台的新聞過濾助理。

使用者不想看到以下 {len(dismissed[-30:])} 篇新聞（已按「不想看」封鎖）：

{lines}

使用者希望看到的是台灣產業的「基本面」與「產業趨勢」新聞：
公司財報、業績、法說會、市場份額、技術突破、重大合作、產業政策等。

使用者不想看到的典型內容：
- 交易訊號（盤後籌碼、技術面、主力動向、即時行情、K線分析）
- 論壇用戶文章（爆料同學會等 UGC 內容）
- 廣告或行銷推廣文
- 與產業基本面無關的市場情緒報導

請從上方封鎖清單中，找出可加入「標題黑名單關鍵字」的詞彙，精準攔截不相關內容、同時避免誤殺正當新聞。

請以純 JSON 回應（不要 markdown code block）：
{{
  "blocked_title_patterns": ["關鍵字1", "關鍵字2"],
  "ai_summary": "一句話說明發現的規律"
}}"""

    try:
        raw = await asyncio.to_thread(claude_client.ask, prompt, 60, None, "claude")
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1] if start != -1 and end > start else raw)
    except Exception as exc:
        log.warning("AI analysis failed: %s", exc)
        return {"skipped": True, "reason": str(exc)}

    bl["rules"]["blocked_title_patterns"] = data.get("blocked_title_patterns", [])
    bl["rules"]["last_analyzed_at"] = datetime.now(timezone.utc).isoformat()
    bl["rules"]["ai_summary"] = data.get("ai_summary", "")
    _save(bl)

    log.info(
        "AI analysis done: %d title patterns — %s",
        len(bl["rules"]["blocked_title_patterns"]),
        bl["rules"]["ai_summary"],
    )
    return data
