"""
Industry Map generator.

Input data:
  1. companies.json filtered by industry  → 已收錄公司
  2. 每家公司的 competitors[]            → 競業擴充池
AI 任務：決定 layout（layered / matrix）、設計 sections、把公司歸位。
輸出落地 data/industry_maps.json。
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services import claude_client, data_store

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
INDUSTRY_MAPS_FILE = DATA_DIR / "industry_maps.json"

BREADTH_PRESETS: dict[str, dict[str, int]] = {
    "narrow": {"n_columns": 3, "max_per_subgroup": 6,  "max_subgroups_per_col": 2},
    "medium": {"n_columns": 5, "max_per_subgroup": 10, "max_subgroups_per_col": 3},
    "broad":  {"n_columns": 7, "max_per_subgroup": 18, "max_subgroups_per_col": 5},
}


# ── Persistence ──────────────────────────────────────────────────────────────

def load_all_maps() -> dict[str, Any]:
    if not INDUSTRY_MAPS_FILE.exists():
        return {}
    try:
        return json.loads(INDUSTRY_MAPS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("industry_maps.json 損毀，重置：%s", exc)
        return {}


def load_map(industry: str) -> dict | None:
    return load_all_maps().get(industry)


def save_map(industry: str, data: dict) -> None:
    all_maps = load_all_maps()
    all_maps[industry] = data
    INDUSTRY_MAPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDUSTRY_MAPS_FILE.write_text(
        json.dumps(all_maps, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def delete_map(industry: str) -> bool:
    all_maps = load_all_maps()
    if industry not in all_maps:
        return False
    del all_maps[industry]
    INDUSTRY_MAPS_FILE.write_text(
        json.dumps(all_maps, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True


# ── Seed data collection ─────────────────────────────────────────────────────

def _extract_one_liner(company: dict) -> str:
    """取一句話描述：優先 blurb 第一段，否則 summary，截 80 字。"""
    text = (company.get("blurb") or company.get("summary") or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:80]


def collect_seed_data(industry: str) -> dict:
    """收集該產業的已收錄公司清單 + 競業擴充池（已去重、去自身）。"""
    all_companies = data_store.get_all_companies()
    in_industry = [c for c in all_companies if (c.get("industry") or "").strip() == industry]

    seen = {data_store.normalize_company_name(c["name"]) for c in in_industry}
    pool: list[dict] = []
    pool_seen: set[str] = set()

    for c in in_industry:
        for comp in (c.get("competitors") or []):
            name = (comp.get("name") or "").strip()
            if not name:
                continue
            norm = data_store.normalize_company_name(name)
            if norm in seen or norm in pool_seen:
                continue
            pool_seen.add(norm)
            pool.append({
                "name": name,
                "tax_id": comp.get("tax_id"),
                "core_biz": comp.get("core_biz", ""),
                "in_db": False,
                "company_id": None,
                "source": c["name"],
            })

    in_db_simple = [
        {
            "company_id": c["id"],
            "name": c["name"],
            "tax_id": c.get("tax_id"),
            "core_biz": _extract_one_liner(c),
            "in_db": True,
        }
        for c in in_industry
    ]
    return {"in_db_companies": in_db_simple, "expansion_pool": pool}


# ── Prompt build ─────────────────────────────────────────────────────────────

def _build_prompt(industry: str, seed: dict, preset: dict) -> str:
    in_db = seed["in_db_companies"]
    pool = seed["expansion_pool"]
    n_cols = preset["n_columns"]
    max_per = preset["max_per_subgroup"]
    max_sub = preset["max_subgroups_per_col"]

    def fmt(c: dict) -> str:
        biz = (c.get("core_biz") or "").strip()
        return f"- {c['name']}" + (f"（{biz}）" if biz else "")

    in_db_list = "\n".join(fmt(c) for c in in_db) or "（無）"
    pool_list = "\n".join(fmt(c) for c in pool[:300]) or "（無）"

    return f"""你是一位資深的台灣產業分析顧問。請為「{industry}」產業設計一張產業地圖（Industry Landscape Map）。

## 已收錄公司清單（必須全部歸位，**一家只放一處**）
{in_db_list}

## 競業擴充池（來自上述公司的競業欄位、含未收錄者）
{pool_list}

## 任務

1. **判定 layout_type**：
   - `layered`：產業有上下層級堆疊（例：硬體基礎 → 邊緣運算 → 通訊協議 → 雲端中台 → 應用層；或上游材料 → 中游製造 → 下游應用）
   - `matrix`：產業為多個平行領域並列、無明顯層級（例：MarTech 拆成廣告/內容/數據/銷售/分析）

2. **設計骨架**：規劃 **{n_cols} 個主分類（column / layer）**，每個底下 1–{max_sub} 個子分類。
   - layered 模式：order 0 在最上層、N 在最下層
   - matrix 模式：order 0 在最左、N 在最右

3. **歸位公司**：
   - 「已收錄公司」**全部**放到合適子分類
   - 從「競業擴充池」挑代表性的**台灣**公司補入
   - 每個子分類最多 {max_per} 家

4. **補充重要玩家**（可選）：若某子分類缺代表性玩家，可額外提名 0–3 家**未收錄但在台灣此領域知名**的公司

## 嚴格規則

- **只列台灣公司**。外國公司（含中、美、日、韓、歐）一律不列，除非該領域台灣完全沒有代表性玩家（例如某些雲端原生技術）。
- `in_db: true` 的公司名字**必須完全一致**地來自上面「已收錄公司清單」，不要改字（含括號股號）
- `company_id` 一律填 `null`，我會 backend lookup
- 不要編造已收錄公司沒提供的資訊
- 用繁體中文

## 輸出（嚴格 JSON，包在 ```json … ``` 內，不要任何外部文字）

```json
{{
  "industry": "{industry}",
  "layout_type": "layered",
  "rationale": "（一句話說明分類邏輯）",
  "sections": [
    {{
      "id": "infra",
      "title": "基礎建設層",
      "order": 0,
      "subgroups": [
        {{
          "title": "IC 設計",
          "companies": [
            {{"name": "聯發科", "in_db": false, "tax_id": null, "company_id": null, "note": "手機晶片龍頭"}},
            {{"name": "稜研科技", "in_db": true, "tax_id": null, "company_id": null}}
          ]
        }}
      ]
    }}
  ]
}}
```
"""


# ── Parse + post-process ─────────────────────────────────────────────────────

def _parse_json_response(raw: str) -> dict:
    if not raw or not raw.strip():
        raise ValueError("AI 回傳為空")

    m = re.search(r"```json\s*(.+?)\s*```", raw, re.DOTALL)
    payload = m.group(1) if m else None
    if payload is None:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"AI 回傳找不到 JSON：{raw[:200]}")
        payload = raw[start:end + 1]

    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        raise ValueError(f"AI 回傳 JSON 解析失敗：{e}；前 200 字：{payload[:200]}")


def _backfill_company_ids(map_data: dict, seed: dict) -> dict:
    in_db_by_norm = {
        data_store.normalize_company_name(c["name"]): c
        for c in seed["in_db_companies"]
    }
    pool_by_norm = {
        data_store.normalize_company_name(c["name"]): c
        for c in seed["expansion_pool"]
    }

    for section in map_data.get("sections", []) or []:
        for sub in section.get("subgroups", []) or []:
            for co in sub.get("companies", []) or []:
                norm = data_store.normalize_company_name(co.get("name", ""))
                hit_db = in_db_by_norm.get(norm)
                if hit_db:
                    co["in_db"] = True
                    co["company_id"] = hit_db["company_id"]
                    co["tax_id"] = hit_db.get("tax_id")
                    co["core_biz"] = hit_db.get("core_biz") or co.get("core_biz") or ""
                else:
                    co["in_db"] = False
                    co["company_id"] = None
                    hit_pool = pool_by_norm.get(norm)
                    if hit_pool:
                        if not co.get("tax_id"):
                            co["tax_id"] = hit_pool.get("tax_id")
                        if not co.get("core_biz"):
                            co["core_biz"] = hit_pool.get("core_biz", "")
    return map_data


# ── Main entry ───────────────────────────────────────────────────────────────

async def generate(
    industry: str,
    breadth: str = "medium",
    *,
    engine: str = "claude",
    progress_cb: Callable[[str], None] | None = None,
) -> dict:
    """生成產業地圖。progress_cb(message) 用於 SSE 推進度。"""
    def emit(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)
        log.info("[industry-map %s] %s", industry, msg)

    preset = BREADTH_PRESETS.get(breadth, BREADTH_PRESETS["medium"])

    emit("收集該產業已收錄公司…")
    seed = collect_seed_data(industry)
    n_in_db = len(seed["in_db_companies"])
    n_pool = len(seed["expansion_pool"])
    emit(f"找到 {n_in_db} 家已收錄公司、{n_pool} 家競業擴充候選")

    if n_in_db == 0:
        raise ValueError(f"產業「{industry}」沒有已收錄公司，無法生成地圖")

    emit(f"呼叫 AI 生成（{breadth} 廣度 / engine={engine}）…可能需 30-90 秒")
    prompt = _build_prompt(industry, seed, preset)

    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(
        None,
        lambda: claude_client.ask(
            prompt,
            timeout=300,
            engine=engine,
        ),
    )

    emit("解析 AI 回應 JSON…")
    parsed = _parse_json_response(raw)

    emit("回填公司 ID…")
    parsed = _backfill_company_ids(parsed, seed)

    sections = parsed.get("sections") or []
    n_nodes = sum(
        len(sub.get("companies", []) or [])
        for s in sections
        for sub in s.get("subgroups", []) or []
    )

    result = {
        "industry": industry,
        "layout_type": parsed.get("layout_type", "matrix"),
        "rationale": parsed.get("rationale", ""),
        "sections": sections,
        "breadth": breadth,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine,
        "stats": {
            "in_db_count": n_in_db,
            "expansion_pool_count": n_pool,
            "rendered_nodes": n_nodes,
        },
    }
    save_map(industry, result)
    emit(f"✓ 完成：共 {len(sections)} 個主分類、{n_nodes} 個公司節點")
    return result
