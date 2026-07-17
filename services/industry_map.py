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

# 地圖生成有兩種模式（由 industryTree 是否有子節點決定，見 generate()）：
#   parent 模式：產業「有子產業」→ 每個子產業當一個 section（可 drill-in），AI 只負責
#                排序子產業 + 選版面，不歸位公司 → prompt 極小、永遠不會 timeout。
#   leaf   模式：產業「無子產業」→ 完整把自己的公司 AI 歸位（原本的行為）。
#
# LEAF_PRESET 是葉模式的版面預設（拿掉了原本的窄/中/廣三選一——縮放改由「你在樹的
# 哪個節點」決定）。已收錄公司數超過版面容量時，generate() 會就地放寬 max_per_subgroup。
# max_pool 限制送進 prompt 的競業擴充池筆數，避免公司多的產業把 prompt 撐爆。
LEAF_PRESET: dict[str, int] = {
    "n_columns": 5, "max_per_subgroup": 10, "max_subgroups_per_col": 3, "max_pool": 40,
}

REPS_PER_CHILD = 5

# subdivide 一次送 AI 分組的公司數上限（與 LEAF_MAX_COMPANIES 同精神的 prompt 保護；
# 超過取資本額前 N 家，其餘留在父產業，之後可對剩餘再跑一次細分）
SUBDIVIDE_MAX_COMPANIES = 150  # 父模式每個子產業顯示幾家代表公司（預覽；點展開看完整葉圖）

# 葉圖（單張完整地圖）能容納的已收錄公司上限——超過就一定會撞 AI timeout，改引導去細分。
LEAF_MAX_COMPANIES = 70


def _industries_of(company: dict) -> list[str]:
    return data_store.company_industries(company)


def _get_children(industry: str) -> list[str]:
    """該產業在 industryTree 中、且確實存在於產業清單裡的子產業（順序依樹）。"""
    tree = data_store.get_industry_tree()
    valid = set(data_store.get_industries())
    return [ch for ch in tree.get(industry, []) if ch in valid]


def _subtree_industries(root: str, tree: dict[str, list[str]]) -> set[str]:
    """root 及其所有後代子產業名。父模式計數要用它遞迴涵蓋整個子樹——某子產業若又被
    細分成孫產業（例：半導體製造 → 晶圓製造與封裝），公司是掛在孫節點上的，只比對子
    產業本名會漏算成 0 家（台積電掛「晶圓製造與封裝」卻沒進「半導體製造」就是這個洞）。"""
    seen: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node in seen:            # 防止樹意外成環時無限迴圈
            continue
        seen.add(node)
        stack.extend(tree.get(node, []))
    return seen


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
    """收集該產業的已收錄公司清單 + 競業擴充池（已去重、去自身）。
    池子依「被幾家已收錄公司列為競業」排序（多家公司獨立提到的候選，比只有一家
    提到的更有代表性），供 _build_prompt 依 breadth 截斷時優先保留重要的。"""
    all_companies = data_store.get_all_companies()
    in_industry = [c for c in all_companies if industry in (_industries_of(c))]

    seen = {data_store.normalize_company_name(c["name"]) for c in in_industry}
    pool_entries: dict[str, dict] = {}
    pool_ref_count: dict[str, int] = {}

    for c in in_industry:
        for comp in (c.get("competitors") or []):
            name = (comp.get("name") or "").strip()
            if not name:
                continue
            norm = data_store.normalize_company_name(name)
            if norm in seen:
                continue
            pool_ref_count[norm] = pool_ref_count.get(norm, 0) + 1
            if norm not in pool_entries:
                pool_entries[norm] = {
                    "name": name,
                    "tax_id": comp.get("tax_id"),
                    "core_biz": comp.get("core_biz", ""),
                    "in_db": False,
                    "company_id": None,
                    "source": c["name"],
                }

    pool = sorted(pool_entries.values(), key=lambda p: -pool_ref_count[data_store.normalize_company_name(p["name"])])

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

def _build_leaf_prompt(industry: str, seed: dict, preset: dict) -> str:
    in_db = seed["in_db_companies"]
    pool = seed["expansion_pool"]
    n_cols = preset["n_columns"]
    max_per = preset["max_per_subgroup"]
    max_sub = preset["max_subgroups_per_col"]

    def fmt(c: dict) -> str:
        biz = (c.get("core_biz") or "").strip()
        return f"- {c['name']}" + (f"（{biz}）" if biz else "")

    max_pool = preset.get("max_pool", 300)
    in_db_list = "\n".join(fmt(c) for c in in_db) or "（無）"
    pool_list = "\n".join(fmt(c) for c in pool[:max_pool]) or "（無）"

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


# ── Parent-mode helpers ──────────────────────────────────────────────────────

def _company_card(c: dict) -> dict:
    """把一筆 companies.json 公司轉成地圖卡片（已收錄）。"""
    return {
        "name": c["name"],
        "in_db": True,
        "company_id": c["id"],
        "tax_id": c.get("tax_id"),
        "core_biz": _extract_one_liner(c),
    }


def _build_parent_prompt(industry: str, child_briefs: list[dict]) -> str:
    """父模式：只要 AI 決定版面 + 排序子產業 + 一句 rationale，不歸位公司 → 極小。"""
    lines = []
    for cb in child_briefs:
        reps = "、".join(cb["rep_names"][:REPS_PER_CHILD]) or "（尚無代表公司）"
        lines.append(f"- {cb['name']}（{cb['count']} 家）：{reps}")
    child_list = "\n".join(lines)
    names_json = json.dumps([cb["name"] for cb in child_briefs], ensure_ascii=False)

    return f"""你是一位資深的台灣產業分析顧問。「{industry}」產業底下已分出以下子領域，
請判斷它們適合用哪種版面呈現、排出先後順序，並用一句話說明分類邏輯。

## 子領域（附家數與代表公司）
{child_list}

## 版面
- `layered`：子領域有上下游 / 層級關係（例：上游材料 → 中游製造 → 下游應用）
- `matrix`：子領域為平行並列、無明顯層級

## 規則
- `order` 陣列**必須剛好包含上面全部子領域名稱、且完全一致**，只是重新排序，不要增刪或改字。
- layered：order 由上游/基礎排到下游/應用；matrix：由左到右。
- 用繁體中文。

## 輸出（嚴格 JSON，包在 ```json … ``` 內，不要任何外部文字）

```json
{{"layout_type": "layered", "rationale": "（一句話）", "order": {names_json}}}
```
"""


async def _generate_parent(
    industry: str, children: list[str], *, engine: str, emit,
) -> dict:
    """父模式：子產業當 sections（可 drill-in），另收一個「未分類」區塊放只掛在本產業、
    尚未細分到任何子產業的公司。AI 只排序子產業，不歸位公司。"""
    all_cos = data_store.get_all_companies()
    tree = data_store.get_industry_tree()

    # 每個子產業的公司（依資本額取代表）。掛在該子產業「或其任一後代孫產業」的都算——
    # 否則被進一步細分過的子產業會顯示 0 家（見 _subtree_industries）。
    child_briefs: list[dict] = []
    child_reps: dict[str, list[dict]] = {}
    child_counts: dict[str, int] = {}
    for ch in children:
        names = _subtree_industries(ch, tree)
        members = [c for c in all_cos if names.intersection(_industries_of(c))]
        members.sort(key=lambda c: c.get("capital", 0) or 0, reverse=True)
        reps = members[:REPS_PER_CHILD]
        child_reps[ch] = reps
        child_counts[ch] = len(members)
        child_briefs.append({"name": ch, "count": len(members), "rep_names": [c["name"] for c in reps]})

    # 只掛在本產業、不在任何子產業的「未分類」公司（公司掛父或掛子二擇一，故精確比對即得）
    orphans = [c for c in all_cos if industry in _industries_of(c)]
    orphans.sort(key=lambda c: c.get("capital", 0) or 0, reverse=True)

    emit(f"子產業 {len(children)} 個、未分類 {len(orphans)} 家；請 AI 排版面順序（快）…")

    layout_type = "matrix"
    rationale = ""
    order = list(children)
    if len(children) >= 2:
        try:
            prompt = _build_parent_prompt(industry, child_briefs)
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(
                None, lambda: claude_client.ask(prompt, timeout=120, engine=engine),
            )
            parsed = _parse_json_response(raw)
            layout_type = "layered" if parsed.get("layout_type") == "layered" else "matrix"
            rationale = parsed.get("rationale", "")
            ai_order = [c for c in (parsed.get("order") or []) if c in children]
            # 補回 AI 漏掉的子產業，維持「全部都在」
            order = ai_order + [c for c in children if c not in ai_order]
        except Exception as e:
            emit(f"排序失敗（{e}），改用預設順序")

    sections: list[dict] = []
    for i, ch in enumerate(order):
        reps = [_company_card(c) for c in child_reps[ch]]
        sections.append({
            "id": f"child:{ch}",
            "title": ch,
            "order": i,
            "drill_to": ch,                 # 前端據此渲染成可展開的子產業磚
            "total_count": child_counts[ch],
            "subgroups": [{"title": "代表公司", "companies": reps}],
        })

    if orphans:
        sections.append({
            "id": "__unclassified__",
            "title": f"未分類（尚未細分為子產業，{len(orphans)} 家）",
            "order": len(order),
            "unclassified": True,
            "total_count": len(orphans),
            "subgroups": [{"title": "", "companies": [_company_card(c) for c in orphans]}],
        })

    n_nodes = sum(len(sub["companies"]) for s in sections for sub in s["subgroups"])
    result = {
        "industry": industry,
        "mode": "parent",
        "layout_type": layout_type,
        "rationale": rationale,
        "sections": sections,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine,
        "stats": {
            "child_count": len(children),
            "unclassified_count": len(orphans),
            "rendered_nodes": n_nodes,
        },
    }
    save_map(industry, result)
    emit(f"✓ 完成：{len(children)} 個子產業" + (f" + 未分類 {len(orphans)} 家" if orphans else ""))
    return result


# ── Subdivision (Phase 2): 把一批公司用 AI 分組成子產業草稿 ─────────────────────

def _build_subdivide_prompt(industry: str, companies: list[dict]) -> str:
    """細分專用的精簡 prompt：只要 AI 回「子產業名 → 成員公司名」，不要完整地圖
    （不要 layout / notes / 巢狀 subgroups / 競業池）。輸出量遠小於完整地圖，避免像
    完整葉圖那樣把上百家公司的輸出撐爆而 timeout。"""
    lines = "\n".join(f"- {c['name']}" + (f"（{c['core_biz']}）" if c.get("core_biz") else "") for c in companies)
    n = len(companies)
    n_groups = max(3, min(8, round(n / 12) or 3))  # 依規模抓 3–8 組
    return f"""你是一位資深的台灣產業分析顧問。以下是「{industry}」產業底下 {n} 家公司，
請把它們分成數個**子產業**（子領域），作為「{industry}」的下一層分類。

## 公司清單
{lines}

## 要求
- 分成約 {n_groups} 個子產業（可 ±2），每個子產業給一個精準、簡潔的名稱。
- **每一家公司都要被分到、且只分到一個子產業**。
- `members` 內的公司名**必須逐字**取自上面清單，不要改字、不要新增清單外的公司。
- 子產業名稱要能作為獨立的產業分類（例：「半導體設計」「電動車」「智慧製造」）。
- 用繁體中文。

## 輸出（嚴格 JSON，包在 ```json … ``` 內，不要任何其他文字）

```json
{{"groups": [
  {{"name": "子產業名", "members": ["公司A", "公司B"]}},
  {{"name": "另一子產業", "members": ["公司C"]}}
]}}
```
"""


async def propose_subdivision(
    industry: str, *, engine: str = "claude", emit=None,
) -> dict:
    """為「還沒分組」的公司（父產業的未分類堆，或無現成葉圖時的全部公司）用 AI 產生
    子產業分組草稿。**不寫入**——只回傳提議，由前端預覽、使用者確認後才 apply。
    回傳 {industry, groups:[{name, company_ids, sample_names, count}]}。"""
    def _emit(msg: str) -> None:
        if emit:
            emit(msg)
        log.info("[subdivide %s] %s", industry, msg)

    all_cos = data_store.get_all_companies()
    targets = [c for c in all_cos if industry in _industries_of(c)]  # 掛本產業標籤的公司
    if not targets:
        raise ValueError(f"「{industry}」沒有可細分的公司")

    # Cap：與 leaf 的 LEAF_MAX_COMPANIES 對稱的保護。太多家會把 prompt 撐爆、
    # 480s 逾時整段白燒（過去 200+ 家實際發生過）。取資本額前 N 家送 AI，
    # 其餘保留在父產業（本來就允許「未歸入」的公司留下，語意一致）。
    if len(targets) > SUBDIVIDE_MAX_COMPANIES:
        targets.sort(key=lambda c: c.get("capital", 0) or 0, reverse=True)
        skipped = len(targets) - SUBDIVIDE_MAX_COMPANIES
        targets = targets[:SUBDIVIDE_MAX_COMPANIES]
        _emit(f"公司多達 {skipped + SUBDIVIDE_MAX_COMPANIES} 家，僅取資本額前 {SUBDIVIDE_MAX_COMPANIES} 家送 AI 分組；其餘 {skipped} 家將留在「{industry}」（可再次細分處理）")

    _emit(f"要細分 {len(targets)} 家公司，呼叫 AI 分組…（{len(targets)} 家約需 {max(1, round(len(targets)/25))}–{max(2, round(len(targets)/18))} 分鐘，請勿關閉）")
    companies = [{"name": c["name"], "core_biz": _extract_one_liner(c)} for c in targets]
    by_norm = {data_store.normalize_company_name(c["name"]): c for c in targets}

    prompt = _build_subdivide_prompt(industry, companies)
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(
        None, lambda: claude_client.ask(prompt, timeout=480, engine=engine),
    )
    parsed = _parse_json_response(raw)

    # 把 AI 回的「子產業名 → 成員公司名」對回 company_id（逐字/正規化比對，比對不到就略過）
    groups: list[dict] = []
    used: set[str] = set()
    for g in parsed.get("groups") or []:
        name = (g.get("name") or "").strip()
        if not name:
            continue
        ids: list[str] = []
        names: list[str] = []
        for member in g.get("members") or []:
            norm = data_store.normalize_company_name((member or "").strip())
            hit = by_norm.get(norm)
            if hit and hit["id"] not in used:
                used.add(hit["id"])
                ids.append(hit["id"])
                names.append(hit["name"])
        if ids:
            groups.append({"name": name, "company_ids": ids, "sample_names": names[:6], "count": len(ids)})

    if not groups:
        raise ValueError("AI 未能產生有效分組，請重試")
    unassigned = len(targets) - len(used)
    _emit(f"✓ 提議 {len(groups)} 個子產業" + (f"（{unassigned} 家未歸入，將保留在「{industry}」）" if unassigned else ""))
    return {"industry": industry, "groups": groups}


def groups_from_sections(sections: list[dict]) -> list[dict]:
    """把地圖 sections 攤成「子產業分組」：section.title 當子產業名，其中的已收錄公司
    （in_db + company_id）當成員。前端的葉圖「收成子產業」也用同一規則（在前端算）。"""
    groups: list[dict] = []
    for s in sections or []:
        ids: list[str] = []
        names: list[str] = []
        for sub in s.get("subgroups", []) or []:
            for co in sub.get("companies", []) or []:
                if co.get("in_db") and co.get("company_id"):
                    ids.append(co["company_id"])
                    names.append(co.get("name", ""))
        title = (s.get("title") or "").strip()
        if ids and title:
            groups.append({"name": title, "company_ids": ids, "sample_names": names[:6], "count": len(ids)})
    return groups


# ── Main entry ───────────────────────────────────────────────────────────────

async def generate(
    industry: str,
    *,
    engine: str = "claude",
    progress_cb: Callable[[str], None] | None = None,
) -> dict:
    """生成產業地圖。有子產業 → 父模式（總覽 + drill-in）；無子產業 → 葉模式（完整歸位）。
    progress_cb(message) 用於 SSE 推進度。"""
    def emit(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)
        log.info("[industry-map %s] %s", industry, msg)

    children = _get_children(industry)
    if children:
        emit(f"「{industry}」底下有 {len(children)} 個子產業，生成總覽地圖…")
        return await _generate_parent(industry, children, engine=engine, emit=emit)

    # ── 葉模式（無子產業）：完整把自己的公司 AI 歸位 ──
    preset = dict(LEAF_PRESET)  # copy: 可能就地調整 max_per_subgroup

    emit("收集該產業已收錄公司…")
    seed = collect_seed_data(industry)
    n_in_db = len(seed["in_db_companies"])
    n_pool = len(seed["expansion_pool"])

    if n_in_db == 0:
        raise ValueError(f"產業「{industry}」沒有已收錄公司，無法生成地圖")

    # 太多公司的單張葉圖必然撞 AI timeout（曾見 205 家 → 逾時）。與其硬跑到 480 秒才失敗，
    # 不如明確擋下、引導去細分。這也擋掉「把大父產業取消細分壓平成一坨」後想硬生成的情況。
    if n_in_db > LEAF_MAX_COMPANIES:
        raise ValueError(
            f"「{industry}」有 {n_in_db} 家已收錄公司，數量太多、無法生成單張完整地圖（會逾時）。"
            f"請改用「✨ 細分成子產業」分層瀏覽（大產業本來就該分層）。"
        )

    # 已收錄公司「必須全部歸位」是硬性規則，若數量超過版面容量
    # （n_columns × max_subgroups_per_col × max_per_subgroup），prompt 會出現自相矛盾的
    # 指令（要求塞下比容量還多的公司），讓 AI 花很多時間在無法滿足的限制上打轉。
    # 這裡就地把 max_per_subgroup 拉高到剛好夠放，不改變 n_columns / 子分類數。
    capacity = preset["n_columns"] * preset["max_subgroups_per_col"] * preset["max_per_subgroup"]
    if n_in_db > capacity:
        slots = preset["n_columns"] * preset["max_subgroups_per_col"]
        preset["max_per_subgroup"] = -(-n_in_db // slots)  # ceil division
        emit(f"已收錄公司數（{n_in_db}）較多，已自動放寬每子分類上限至 {preset['max_per_subgroup']} 家")

    n_pool_used = min(n_pool, preset.get("max_pool", 300))
    if n_pool_used < n_pool:
        emit(f"找到 {n_in_db} 家已收錄公司、{n_pool} 家競業擴充候選（取前 {n_pool_used} 家最具代表性的送 AI）")
    else:
        emit(f"找到 {n_in_db} 家已收錄公司、{n_pool} 家競業擴充候選")

    emit(f"呼叫 AI 生成（engine={engine}）…公司數量較多時可能需數分鐘")
    prompt = _build_leaf_prompt(industry, seed, preset)

    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(
        None,
        lambda: claude_client.ask(
            prompt,
            timeout=480,
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
        "mode": "leaf",
        "layout_type": parsed.get("layout_type", "matrix"),
        "rationale": parsed.get("rationale", ""),
        "sections": sections,
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
