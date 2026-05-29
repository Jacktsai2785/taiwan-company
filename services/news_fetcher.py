"""Fetch recent Taiwan news articles from Google News RSS."""
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

log = logging.getLogger(__name__)

TAIWAN_TZ = timezone(timedelta(hours=8))

# Mainland-China sources to drop even when Google News occasionally surfaces them
_BLOCKED_SOURCES = {
    "人民日報", "新華社", "環球時報", "中央電視台", "CCTV", "央視新聞",
    "觀察者網", "鳳凰網", "中新社", "海峽導報", "福建日報", "大公報",
}

# Stock discussion forums and low-signal aggregators — user posts masquerading as news
_FORUM_SOURCES = {
    "股市爆料同學會", "Cmoney股市爆料同學會", "CMoney股市爆料同學會",
    "PTT Stock", "Mobile01",
}

# Title substrings that indicate forum / UGC content regardless of source field
_TITLE_NOISE_PATTERNS = (
    "股市爆料同學會",
    "爆料同學會",
)

# Domains whose URLs produce trading-signal / low-quality content
_BLOCKED_DOMAINS = {
    "cmnews.com.tw",
}

# Synonym expansion for abstract industry categories — broadens the news query
# so vague labels like 「前瞻科技」 actually return real articles.
_INDUSTRY_SYNONYMS: dict[str, list[str]] = {
    "前瞻科技": ["人工智慧", "AI晶片", "AI伺服器", "半導體", "晶片", "量子運算", "5G", "6G"],
    "消費生活": ["零售業", "餐飲業", "電商", "消費市場"],
    "綠色永續": ["永續", "ESG", "減碳", "碳權", "再生能源"],
    "環保":     ["永續", "ESG", "減碳", "碳權", "再生能源"],
    "半導體":   ["IC設計", "晶片", "晶圓代工"],
    "金融":     ["銀行", "保險", "證券"],
    "生技":     ["生技", "醫療", "製藥"],
    "電動車":   ["電動車", "EV電池"],
}


# ── Time window ────────────────────────────────────────────────────────────────

def news_window() -> tuple[datetime, datetime]:
    """Window: yesterday 00:00 TW → now (~24-48 h rolling)."""
    now = datetime.now(TAIWAN_TZ)
    start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


def cache_date() -> str:
    """Date label for today's digest cache key (e.g. '2025-05-04')."""
    now = datetime.now(TAIWAN_TZ)
    today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
    ref = now if now >= today_8am else now - timedelta(days=1)
    return ref.strftime("%Y-%m-%d")


# ── Public fetchers ────────────────────────────────────────────────────────────

def _get_synonyms(industry: str) -> list[str]:
    """Merge hardcoded synonyms with any AI-generated keywords from persistent store."""
    from services.data_store import get_keywords_for_industry
    hardcoded = _INDUSTRY_SYNONYMS.get(industry, [])
    persisted = get_keywords_for_industry(industry)
    seen: set[str] = set(hardcoded)
    merged = list(hardcoded)
    for k in persisted:
        if k not in seen:
            seen.add(k)
            merged.append(k)
    return merged


async def fetch_industry_news(
    industry: str, max_articles: int = 40, days: int = 1
) -> list[dict]:
    """Fetch Taiwan news for the industry, expanding with synonyms when available.

    We rely on ceid=TW:zh-Hant (Taiwan edition) for region filtering instead of
    appending '台灣' as a content keyword — the latter silently drops articles from
    Taiwanese sources that cover global stories without mentioning '台灣' explicitly.
    `days` controls the when:Nd recency hint passed to Google News.
    """
    synonyms = _get_synonyms(industry)
    when = f"when:{days}d"
    if synonyms:
        terms = [industry, *synonyms]
        query = f"({' OR '.join(terms)}) {when}"
    else:
        query = f"{industry} {when}"
    return await _fetch_rss(query, max_articles, label=industry)


async def fetch_company_news(company_names: list[str], max_articles: int = 15) -> list[dict]:
    """Fetch Taiwan news mentioning any of the given companies.

    Google News' quoted search is fuzzy — it will surface articles that share
    only a single character with the company name (e.g. "赫" vs. "赫侖"), which
    pollutes results with unrelated entertainment/politics. We post-filter so
    only articles whose title contains a full company name remain.

    Caps at the first 10 names so the query URL stays reasonable, and skips
    names shorter than 2 chars (too generic to disambiguate).
    """
    cleaned = [
        n.replace("股份有限公司", "").replace("有限公司", "").strip()
        for n in company_names
    ]
    cleaned = [n for n in cleaned if len(n) >= 2][:10]
    if not cleaned:
        return []
    quoted = [f'"{n}"' for n in cleaned]
    query = f"({' OR '.join(quoted)})"
    raw = await _fetch_rss(query, max_articles * 3, label=f"companies({len(cleaned)})")

    matched: list[dict] = []
    for a in raw:
        title = a.get("title", "")
        if any(n in title for n in cleaned):
            matched.append(a)
            if len(matched) >= max_articles:
                break
    log.info("Watchlist post-filter: %d → %d articles", len(raw), len(matched))
    return matched


# ── Internal RSS fetch ─────────────────────────────────────────────────────────

async def _fetch_rss(query: str, max_articles: int, label: str = "") -> list[dict]:
    from services.blacklist import get_dynamic_filters
    dyn_urls, dyn_domains, dyn_sources, dyn_title_patterns = get_dynamic_filters()

    start, end = news_window()
    rss_url = (
        "https://news.google.com/rss/search"
        f"?q={urllib.parse.quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(rss_url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        content = resp.text
    except Exception as exc:
        log.warning("RSS fetch failed (%s): %s", label, exc)
        return []

    feed = feedparser.parse(content)
    articles: list[dict] = []

    for entry in feed.entries:
        try:
            pub_utc = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            pub_tw = pub_utc.astimezone(TAIWAN_TZ)
        except Exception:
            continue

        if not (start <= pub_tw <= end):
            continue

        source = getattr(getattr(entry, "source", None), "title", "") or ""
        if not source and " - " in (entry.title or ""):
            source = entry.title.rsplit(" - ", 1)[-1].strip()

        if any(b in source for b in _BLOCKED_SOURCES):
            continue
        if any(f in source for f in _FORUM_SOURCES):
            continue
        if source in dyn_sources:
            continue

        url = entry.link or ""
        if any(d in url for d in _BLOCKED_DOMAINS):
            continue
        if any(d in url for d in dyn_domains):
            continue
        if url in dyn_urls:
            continue

        title = entry.title or ""
        if any(p in title for p in _TITLE_NOISE_PATTERNS):
            continue
        if any(p in title for p in dyn_title_patterns):
            continue
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()

        articles.append({
            "title": title,
            "url": entry.link or "",
            "source": source,
            "published_at": pub_tw.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        })

        if len(articles) >= max_articles:
            break

    log.info("Fetched %d articles for %s (window %s–%s)", len(articles), label, start, end)
    return articles
