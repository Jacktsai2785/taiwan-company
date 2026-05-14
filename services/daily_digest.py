"""Generate and cache the daily industry news digest using Claude AI."""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.claude_client import ask
from services.news_fetcher import (
    cache_date,
    fetch_company_news,
    fetch_industry_news,  # also used in _generate_trends bootstrap
)

log = logging.getLogger(__name__)

TAIWAN_TZ = timezone(timedelta(hours=8))
_DIGEST_PATH = Path("data/daily_digest.json")
_TRENDS_PATH = Path("data/industry_trends.json")
_GENERATING: set[str] = set()
_TRENDS_GENERATING: set[str] = set()

WATCHLIST_TOPIC = "感興趣名單"
PRUNE_DAYS = 90
MIN_DAYS_FOR_TRENDS = 1   # bootstrap fetches 7-day RSS when cache is sparse


# ── Cache I/O ──────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(_DIGEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(cache: dict) -> None:
    _DIGEST_PATH.parent.mkdir(exist_ok=True)
    _DIGEST_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_trends() -> dict:
    try:
        return json.loads(_TRENDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_trends(data: dict) -> None:
    _TRENDS_PATH.parent.mkdir(exist_ok=True)
    _TRENDS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _prune(cache: dict, industry: str) -> None:
    """Drop digest entries older than PRUNE_DAYS for this industry."""
    entries = cache.get(industry, {})
    cutoff = (datetime.now(TAIWAN_TZ) - timedelta(days=PRUNE_DAYS)).strftime("%Y-%m-%d")
    stale = [d for d in list(entries) if d < cutoff]
    for d in stale:
        del entries[d]


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_digest(
    industry: str,
    api_key: str = "",
    provider: str = "anthropic",
    force_refresh: bool = False,
) -> dict:
    today = cache_date()
    if not force_refresh:
        entry = _load().get(industry, {}).get(today)
        if entry:
            return entry
    return await _generate(industry, today, api_key, provider)


async def refresh_all_digests() -> None:
    from services.data_store import get_industries
    from services.jk_nb_exporter import export_industry_digest_to_jk_nb
    for ind in get_industries():
        try:
            log.info("Scheduler: refreshing digest for %s", ind)
            digest = await _generate(ind, cache_date(), api_key="", provider="anthropic")
            try:
                export_industry_digest_to_jk_nb(ind, digest)
            except Exception:
                log.exception("jk_nb export failed for %s (non-fatal)", ind)
        except Exception as exc:
            log.warning("Scheduler: digest failed for %s: %s", ind, exc)


async def get_trends(
    industry: str,
    api_key: str = "",
    provider: str = "anthropic",
    force_refresh: bool = False,
) -> dict:
    existing = _load_trends().get(industry)
    if existing and not force_refresh:
        return existing
    return await _generate_trends(industry, api_key, provider)


async def refresh_all_trends() -> None:
    from services.data_store import get_industries
    for ind in get_industries():
        try:
            log.info("Scheduler: refreshing trends for %s", ind)
            await _generate_trends(ind, api_key="", provider="anthropic")
        except Exception as exc:
            log.warning("Scheduler: trends failed for %s: %s", ind, exc)


async def _generate_trends(industry: str, api_key: str, provider: str) -> dict:
    if industry in _TRENDS_GENERATING:
        for _ in range(60):
            await asyncio.sleep(2)
            result = _load_trends().get(industry)
            if result:
                return result
        raise TimeoutError("趨勢生成超時，請稍後再試")

    _TRENDS_GENERATING.add(industry)
    try:
        cache = _load()
        entries = cache.get(industry, {})
        dates = sorted(entries.keys())[-PRUNE_DAYS:]

        if len(dates) < MIN_DAYS_FOR_TRENDS:
            result = {
                "industry": industry,
                "generated_at": datetime.now(TAIWAN_TZ).isoformat(),
                "date_range": {"from": dates[0] if dates else "", "to": dates[-1] if dates else ""},
                "days_analyzed": len(dates),
                "overview": f"目前累積 {len(dates)} 天資料，至少需要 {MIN_DAYS_FOR_TRENDS} 天才能生成趨勢分析。",
                "trends": [],
            }
            store = _load_trends()
            store[industry] = result
            _save_trends(store)
            return result

        # Build per-day summary lines and collect article title frequencies + URLs
        summary_lines: list[str] = []
        title_info: dict[str, dict] = {}  # title -> {count, url, source}

        def _record_title(title: str, url: str, source: str) -> None:
            title = (title or "").strip()
            if not title:
                return
            info = title_info.get(title)
            if info is None:
                title_info[title] = {"count": 1, "url": url or "", "source": source or ""}
            else:
                info["count"] += 1
                # Fill in url/source if a later article has them and earlier didn't
                if not info["url"] and url:
                    info["url"] = url
                if not info["source"] and source:
                    info["source"] = source

        for d in dates:
            entry = entries[d]
            topic_names = [
                t["name"] for t in entry.get("topics", [])
                if t["name"] != WATCHLIST_TOPIC
            ]
            topics_str = "、".join(topic_names) if topic_names else "（無主題）"
            summary_text = (entry.get("summary") or "").strip()
            if summary_text:
                summary_lines.append(f"{d} | 主題：{topics_str} | {summary_text}")
            for t in entry.get("topics", []):
                if t["name"] == WATCHLIST_TOPIC:
                    continue
                for a in t.get("articles", []):
                    _record_title(a.get("title", ""), a.get("url", ""), a.get("source", ""))

        # Bootstrap: when cached days < 7, supplement title pool with a fresh 7-day fetch
        # so the first-run trend has meaningful data even before the daily digest accumulates.
        BOOTSTRAP_THRESHOLD = 7
        if len(dates) < BOOTSTRAP_THRESHOLD:
            log.info("Trends bootstrap: only %d cached days, fetching 7-day titles for %s", len(dates), industry)
            try:
                bootstrap_arts = await fetch_industry_news(industry, max_articles=80, days=7)
                for a in bootstrap_arts:
                    _record_title(a.get("title", ""), a.get("url", ""), a.get("source", ""))
            except Exception as exc:
                log.warning("Bootstrap fetch failed for %s: %s", industry, exc)

        top_titles = sorted(title_info.items(), key=lambda x: -x[1]["count"])[:50]
        summaries_block = "\n".join(summary_lines)
        titles_block = "\n".join(
            f"{i+1}. {title}（{info['count']}天）" for i, (title, info) in enumerate(top_titles)
        )
        date_from, date_to, n = dates[0], dates[-1], len(dates)

        prompt = f"""以下是過去 {n} 天（{date_from} 至 {date_to}）台灣「{industry}」產業的新聞摘要與高頻標題：

【每日摘要與主題】
{summaries_block}

【高頻出現文章標題（去重，依出現天數排序）】
{titles_block}

請完成：
1. 寫一段 100-150 字的季度趨勢總覽，指出這段期間的主要動態與方向，繁體中文，財經分析口吻。
2. 列出 3-5 個明顯趨勢，每個趨勢包含：名稱（8字以內）、40-60字洞察、信號（rising/falling/stable）、最具代表性的 1-2 條標題（從上方清單選）。

只回傳此 JSON（無其他文字）：
{{
  "overview": "總覽文字",
  "trends": [
    {{
      "name": "趨勢名稱",
      "insight": "洞察文字",
      "signal": "rising",
      "representative_titles": ["標題1"]
    }}
  ]
}}"""

        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None,
            lambda: ask(prompt, timeout=180, api_key=api_key, provider=provider),
        )

        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError(f"Claude 趨勢回傳格式無法解析：{raw[:300]}")
        parsed = json.loads(m.group())

        # Enrich representative_titles (string list from Claude) with url + source,
        # so the frontend can render them as clickable links to the original news.
        enriched_trends: list[dict] = []
        for t in parsed.get("trends", []):
            titles = t.get("representative_titles", []) or []
            enriched: list[dict] = []
            for title in titles:
                if not isinstance(title, str):
                    continue
                title = title.strip()
                if not title:
                    continue
                info = title_info.get(title)
                if info is None:
                    # Try a loose match: Claude may have lightly rephrased a title
                    for cached_title, cached_info in top_titles:
                        if title in cached_title or cached_title in title:
                            info = cached_info
                            title = cached_title
                            break
                enriched.append({
                    "title": title,
                    "url": (info or {}).get("url", ""),
                    "source": (info or {}).get("source", ""),
                })
            enriched_trends.append({**t, "representative_titles": enriched})

        result = {
            "industry": industry,
            "generated_at": datetime.now(TAIWAN_TZ).isoformat(),
            "date_range": {"from": date_from, "to": date_to},
            "days_analyzed": n,
            "overview": parsed.get("overview", ""),
            "trends": enriched_trends,
        }
        store = _load_trends()
        store[industry] = result
        _save_trends(store)
        return result
    finally:
        _TRENDS_GENERATING.discard(industry)


async def generate_industry_keywords(
    industry: str,
    api_key: str = "",
    provider: str = "anthropic",
) -> list[str]:
    """Ask Claude to suggest 5-8 search keywords for the industry, then persist them."""
    from services.data_store import save_industry_keywords

    prompt = f"""你是台灣財經新聞編輯。請為「{industry}」產業，提供 5-8 個繁體中文搜尋關鍵字，
用於向 Google News 查詢台灣相關產業新聞。關鍵字應具體、有辨識度，避免過於通用。

只回傳 JSON 陣列（無其他文字）：
["關鍵字1", "關鍵字2", "關鍵字3"]"""

    loop = asyncio.get_event_loop()
    try:
        raw = await loop.run_in_executor(
            None,
            lambda: ask(prompt, timeout=60, api_key=api_key, provider=provider),
        )
        import re
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not m:
            raise ValueError(f"無法解析關鍵字回傳：{raw[:200]}")
        keywords: list[str] = json.loads(m.group())
        keywords = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
    except Exception as exc:
        log.warning("generate_industry_keywords failed for %s: %s", industry, exc)
        keywords = []

    if keywords:
        save_industry_keywords(industry, keywords)
        log.info("Saved %d keywords for %s: %s", len(keywords), industry, keywords)
    return keywords


# ── Internal ───────────────────────────────────────────────────────────────────

async def _generate(industry: str, today: str, api_key: str, provider: str) -> dict:
    if industry in _GENERATING:
        for _ in range(60):
            await asyncio.sleep(2)
            entry = _load().get(industry, {}).get(today)
            if entry:
                return entry
        raise TimeoutError("日報生成超時，請稍後再試")

    _GENERATING.add(industry)
    try:
        from services.data_store import get_all_companies
        watchlist_names = [
            c["name"] for c in get_all_companies() if c.get("industry") == industry
        ]

        industry_arts, company_arts = await asyncio.gather(
            fetch_industry_news(industry),
            fetch_company_news(watchlist_names),
        )

        # If the same URL appears in both lists, treat it as a watchlist article
        company_urls = {a["url"] for a in company_arts if a.get("url")}
        industry_only = [a for a in industry_arts if a.get("url") not in company_urls]

        total = len(industry_only) + len(company_arts)
        if total == 0:
            result = _empty_result(industry, today, has_watchlist=bool(watchlist_names))
        else:
            result = await _summarize(
                industry, industry_only, company_arts, today, api_key, provider
            )

        cache = _load()
        cache.setdefault(industry, {})[today] = result
        _prune(cache, industry)
        _save(cache)
        return result
    finally:
        _GENERATING.discard(industry)


def _empty_result(industry: str, today: str, has_watchlist: bool) -> dict:
    msg = f"今日尚無「{industry}」產業相關新聞"
    if has_watchlist:
        msg += "，您追蹤的公司今日也未有報導"
    return {
        "date": today,
        "generated_at": datetime.now(TAIWAN_TZ).isoformat(),
        "summary": msg + "。",
        "topics": [],
        "article_count": 0,
    }


async def _summarize(
    industry: str,
    industry_arts: list[dict],
    company_arts: list[dict],
    today: str,
    api_key: str,
    provider: str,
) -> dict:
    sections: list[str] = []
    if industry_arts:
        sections.append("【產業相關報導】（請分類）")
        sections += [
            f"{i + 1}. {a['title']} [{a['source']}]"
            for i, a in enumerate(industry_arts)
        ]
    if company_arts:
        sections.append("\n【您關注的公司報導】（不需分類，僅供摘要參考）")
        sections += [f"- {a['title']} [{a['source']}]" for a in company_arts]

    titles_block = "\n".join(sections)
    industry_count = len(industry_arts)

    prompt = f"""以下是今日台灣「{industry}」產業的新聞清單（僅來自台灣媒體）：

{titles_block}

請完成兩件事：
1. 寫一段 60-100 字的今日產業摘要，涵蓋上述所有報導重點，繁體中文，財經編輯口吻。
2. 將「產業相關報導」分類成 2-5 個主題（如供應鏈、市場動態、法說財報、政策法規、人事異動）；「您關注的公司報導」不需分類。

只回傳此 JSON（無其他文字）：
{{
  "summary": "摘要文字",
  "topics": [
    {{ "name": "主題名稱", "article_indices": [1, 3, 5] }}
  ]
}}

注意：article_indices 只能引用「產業相關報導」的編號（1 到 {industry_count}）。若無產業相關報導，topics 給空陣列。"""

    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(
        None,
        lambda: ask(prompt, timeout=120, api_key=api_key, provider=provider),
    )

    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"Claude 回傳格式無法解析：{raw[:300]}")
    parsed = json.loads(m.group())

    topics: list[dict] = []
    # Prepend the watchlist topic so it shows up as the first pill
    if company_arts:
        topics.append({"name": WATCHLIST_TOPIC, "articles": company_arts})

    for t in parsed.get("topics", []):
        # Skip if Claude accidentally re-uses the watchlist topic name
        if t.get("name") == WATCHLIST_TOPIC:
            continue
        arts = [
            industry_arts[i - 1]
            for i in t.get("article_indices", [])
            if 1 <= i <= len(industry_arts)
        ]
        if arts:
            topics.append({"name": t["name"], "articles": arts})

    return {
        "date": today,
        "generated_at": datetime.now(TAIWAN_TZ).isoformat(),
        "summary": parsed.get("summary", ""),
        "topics": topics,
        "article_count": len(industry_arts) + len(company_arts),
    }
