"""
services/jk_nb_exporter.py — push markdown snapshots to ~/jk_nb/raw/_dropbox/

Optional integration. If jk_nb vault dropbox doesn't exist, all functions
silently no-op so taiwan-company keeps working in environments without jk_nb.

Two exports:
  - export_company_to_jk_nb(company)         → company-profile markdown
  - export_industry_digest_to_jk_nb(industry, digest) → industry-digest markdown

Both write to ~/jk_nb/raw/_dropbox/ ; jk_nb's raw-watcher will pick them up,
move into raw/, and the nightly consume timer compiles them into wiki/.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

JK_NB_DROPBOX = Path.home() / "jk_nb" / "raw" / "_dropbox"


def _slugify(text: str, max_len: int = 60) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w一-鿿-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-_")
    return text[:max_len].rstrip("-_") or "untitled"


def _yaml_quote(s: str) -> str:
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _write(path: Path, content: str) -> Path | None:
    if not JK_NB_DROPBOX.exists():
        return None  # jk_nb not present in this env — silent no-op
    if path.exists():
        i = 2
        stem, suffix = path.stem, path.suffix
        while True:
            candidate = path.with_name(f"{stem}-{i}{suffix}")
            if not candidate.exists():
                path = candidate
                break
            i += 1
    path.write_text(content, encoding="utf-8")
    log.info("jk_nb export: wrote %s", path)
    return path


def export_company_to_jk_nb(company: dict) -> Path | None:
    """Dump an enriched company as markdown.

    Skips if summary is empty (avoids exporting half-built pre-enrich state).
    Returns the written path, or None if skipped / jk_nb absent.
    """
    name = (company.get("name") or "").strip()
    summary = (company.get("summary") or "").strip()
    if not name or not summary:
        return None

    today = date.today().isoformat()
    slug = _slugify(name)
    filename = f"{today}-company-{slug}.md"

    fm = (
        "---\n"
        f"source: {_yaml_quote('taiwan-company:' + (company.get('id') or name))}\n"
        f"captured: {today}\n"
        "type: company-profile\n"
        f"title: {_yaml_quote(name)}\n"
        f"tax_id: {_yaml_quote(company.get('tax_id') or '')}\n"
        f"industry: {_yaml_quote(company.get('industry') or '')}\n"
        "---\n\n"
    )

    body = [f"# {name}", ""]
    facts = []
    if company.get("listing_status"):
        facts.append(f"**狀態**: {company['listing_status']}")
    if company.get("tax_id"):
        facts.append(f"**統一編號**: {company['tax_id']}")
    if company.get("representative"):
        facts.append(f"**代表人**: {company['representative']}")
    if company.get("authorized_capital"):
        facts.append(f"**資本總額**: NT$ {company['authorized_capital']:,}")
    if company.get("capital"):
        facts.append(f"**實收資本額**: NT$ {company['capital']:,}")
    if company.get("address"):
        facts.append(f"**所在地**: {company['address']}")
    if company.get("setup_date"):
        facts.append(f"**設立日期**: {company['setup_date']}")
    if company.get("industry"):
        facts.append(f"**產業**: {company['industry']}")
    if facts:
        body.extend(facts + [""])

    directors = company.get("directors") or []
    if directors:
        body.append("## 董監事")
        body.append("")
        body.append("| 職稱 | 姓名 | 所代表法人 | 持股比例 |")
        body.append("|---|---|---|---|")
        for d in directors:
            ratio = f"{d['ratio']*100:.2f}%" if d.get("ratio") is not None else "—"
            body.append(
                f"| {d.get('title') or '—'} "
                f"| {d.get('name') or '—'} "
                f"| {d.get('representative_of') or '—'} "
                f"| {ratio} |"
            )
        body.append("")

    body.extend(["## 公司簡介", "", summary, ""])

    return _write(JK_NB_DROPBOX / filename, fm + "\n".join(body))


def export_industry_digest_to_jk_nb(industry: str, digest: dict) -> Path | None:
    """Dump a daily industry digest as markdown.

    Skips if summary is empty or is the boilerplate "no news today" message.
    """
    summary = (digest.get("summary") or "").strip()
    if not summary or summary.startswith("今日尚無"):
        return None

    digest_date = digest.get("date") or date.today().isoformat()
    slug = _slugify(industry)
    filename = f"{digest_date}-news-{slug}.md"

    fm = (
        "---\n"
        f"source: {_yaml_quote(f'taiwan-company:digest:{industry}')}\n"
        f"captured: {digest_date}\n"
        "type: industry-digest\n"
        f"title: {_yaml_quote(f'{industry} 每日摘要 — {digest_date}')}\n"
        f"industry: {_yaml_quote(industry)}\n"
        f"generated_at: {_yaml_quote(digest.get('generated_at') or datetime.now().isoformat())}\n"
        "---\n\n"
    )

    body = [f"# {industry} 每日摘要 — {digest_date}", "", summary, ""]

    sources_raw = digest.get("sources") or digest.get("articles") or []
    if isinstance(sources_raw, list) and sources_raw:
        body.append("## 來源")
        body.append("")
        for s in sources_raw[:30]:
            if isinstance(s, dict):
                title = s.get("title") or s.get("name") or "(untitled)"
                url = s.get("url") or s.get("link") or ""
                body.append(f"- [{title}]({url})" if url else f"- {title}")
            else:
                body.append(f"- {s}")
        body.append("")

    return _write(JK_NB_DROPBOX / filename, fm + "\n".join(body))


def __main_smoke_test() -> None:
    """Manual smoke test: python -m services.jk_nb_exporter"""
    import sys
    sample_company = {
        "id": "test-001",
        "name": "測試科技股份有限公司",
        "tax_id": "12345678",
        "representative": "王小明",
        "industry": "資訊科技",
        "listing_status": "上市",
        "authorized_capital": 100_000_000,
        "capital": 80_000_000,
        "address": "台北市信義區",
        "setup_date": "2010-01-15",
        "directors": [
            {"title": "董事長", "name": "王小明", "ratio": 0.35},
            {"title": "董事", "name": "李大華", "representative_of": "某某投資", "ratio": 0.15},
        ],
        "summary": "這是測試 summary。\n\n## 業務概況\n\n- 主營軟體開發\n- 客戶包含...",
    }
    sample_digest = {
        "date": date.today().isoformat(),
        "summary": "今日該產業出現 3 則重點新聞...",
        "generated_at": datetime.now().isoformat(),
        "sources": [
            {"title": "範例新聞 A", "url": "https://example.com/a"},
            {"title": "範例新聞 B", "url": "https://example.com/b"},
        ],
    }
    print(f"jk_nb dropbox: {JK_NB_DROPBOX} (exists: {JK_NB_DROPBOX.exists()})")
    p1 = export_company_to_jk_nb(sample_company)
    print(f"company → {p1}")
    p2 = export_industry_digest_to_jk_nb("資訊科技", sample_digest)
    print(f"digest  → {p2}")
    sys.exit(0)


if __name__ == "__main__":
    __main_smoke_test()
