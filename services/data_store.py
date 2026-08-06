import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent.parent / "data"
COMPANIES_FILE = DATA_DIR / "companies.json"
CONFIG_FILE = DATA_DIR / "config.json"
KEYWORDS_FILE = DATA_DIR / "industry_keywords.json"

DEFAULT_COMPANIES = {"companies": []}
DEFAULT_CONFIG = {"industries": ["前瞻科技", "消費生活", "環保"], "labels": []}

# 序列化「讀整檔→改→寫整檔」的臨界區。FastAPI 的同步(def)路由在 threadpool 跑，
# 多執行緒會同時 read-modify-write 同一個 JSON，無鎖會 lost update。用 RLock 讓
# 互相呼叫的 mutator（如 update_company→upsert_company）可重入。
_LOCK = threading.RLock()

# 檔案層 mtime 快取：companies.json 已近 10MB，每次 _read 整檔 parse 約 27ms，
# 而 get_company / find_company_by_name* 在 routers 有 40+ 個呼叫點，等於每個請求
# 都重複付這筆錢。檔案沒變（st_mtime_ns 相同）就直接回快取物件；_write 落地後同步
# 更新快取。外部程序改檔（還原備份、手動編輯）靠 mtime 變化自動失效。
# 注意：快取物件是共享參照——讀取路徑一律視為唯讀；單筆修改請走 get_company
# （回傳 deepcopy）→ 改 → upsert_company 的流程，不要就地改 get_all_companies 的結果。
_CACHE_LOCK = threading.Lock()
_FILE_CACHE: dict[Path, tuple[int, dict]] = {}


def _read(path: Path, default: dict) -> dict:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    mtime_ns = path.stat().st_mtime_ns
    with _CACHE_LOCK:
        hit = _FILE_CACHE.get(path)
        if hit and hit[0] == mtime_ns:
            return hit[1]
    data = json.loads(path.read_text(encoding="utf-8"))
    # 若 stat 與 read 之間檔案被替換：這裡會以「舊 mtime + 新內容」入快取，
    # 下次 stat 發現 mtime 不同會重新讀——只會多讀一次，不會讀到舊資料。
    with _CACHE_LOCK:
        _FILE_CACHE[path] = (mtime_ns, data)
    return data


def _write(path: Path, data: dict) -> None:
    """原子寫：先寫同目錄的 .tmp 再 os.replace（POSIX 保證 rename 原子）。
    讀者永遠看到完整的舊檔或完整的新檔，不會讀到寫一半的壞 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        with _CACHE_LOCK:
            _FILE_CACHE[path] = (path.stat().st_mtime_ns, data)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# Public aliases: this module's atomic read/write is generic (path + dict), so
# other services (blacklist.py, daily_digest.py) that keep their own small JSON
# files reuse it here instead of hand-rolling a non-atomic json.dump.
read_json = _read
write_json = _write


# --- Companies ---

def _ensure_industries_field(companies: list[dict]) -> tuple[list[dict], bool]:
    """One-time migration: industry (str) → industries (list). Returns (companies, changed)."""
    changed = False
    for c in companies:
        if "industries" not in c:
            old = c.get("industry") or ""
            c["industries"] = [old] if old else []
            changed = True
    return companies, changed


def get_all_companies() -> list[dict]:
    with _LOCK:
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        companies, changed = _ensure_industries_field(store["companies"])
        if changed:
            store["companies"] = companies
            _write(COMPANIES_FILE, store)
        return companies


def company_industries(c: dict) -> list[str]:
    """公司的產業別清單（相容舊 industry(str) 欄位）。單一來源，取代散落 9+ 處、
    版本還不一致（config.py 曾用 ['']  會塞空產業）的 inline 相容讀法。"""
    inds = c.get("industries")
    if inds:
        return list(inds)
    old = c.get("industry")
    return [old] if old else []


def get_company(company_id: str) -> dict | None:
    """回傳 deepcopy：呼叫端普遍走「取出 → 就地改 → upsert」流程，若回共享的
    快取物件，改到一半就會被其他請求讀到（甚至在不寫入時汙染快取）。單筆 copy
    很便宜（平均 ~20KB），upsert 時整筆替換回去。"""
    import copy
    hit = next((c for c in get_all_companies() if c.get("id") == company_id), None)
    return copy.deepcopy(hit) if hit is not None else None


def find_company_by_name(name: str) -> dict | None:
    return next((c for c in get_all_companies() if c.get("name") == name), None)


def normalize_company_name(name: str) -> str:
    """Strip company-type suffix to match short and full names interchangeably."""
    n = (name or "").strip()
    for sfx in ("股份有限公司", "有限公司"):
        if n.endswith(sfx):
            return n[: -len(sfx)]
    return n


_STOCK_SUFFIX_RE = re.compile(r"[（(]\d{4,6}[）)]\s*$")


def clean_company_name(name: str) -> str:
    """去除名稱尾端的「（股號）」——AI 生成的產業地圖競業池/競業表慣用
    「美琪瑪國際股份有限公司（4721）」寫法，從地圖點加入或上傳抽名時原樣入庫，
    會讓 GCIS/TWSE 全部比對不到 → 統編/代表人空白、上市狀態誤判、DD memo
    拿殘料生成到超時。入庫前一律清掉；地圖上的顯示不受影響。"""
    n = (name or "").strip()
    while True:
        m = _STOCK_SUFFIX_RE.search(n)
        if not m:
            return n
        n = n[: m.start()].strip()


def find_company_by_name_or_tax_id(name: str, tax_id: str = "") -> dict | None:
    """Match by tax_id first (exact), then by normalized name (suffix-tolerant)."""
    companies = get_all_companies()
    if tax_id:
        hit = next((c for c in companies if c.get("tax_id") == tax_id), None)
        if hit:
            return hit
    if name:
        target = normalize_company_name(name)
        if target:
            return next(
                (c for c in companies if normalize_company_name(c.get("name") or "") == target),
                None,
            )
    return None


def upsert_company(company: dict) -> dict:
    with _LOCK:
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        companies = store["companies"]
        idx = next((i for i, c in enumerate(companies) if c.get("id") == company["id"]), None)
        if idx is not None:
            companies[idx] = company
        else:
            companies.append(company)
        _write(COMPANIES_FILE, store)
        return company


def create_company(name: str, label: str, industry: str = "", tax_id: str = "") -> dict:
    inds = [industry] if industry else []
    company = {
        "id": str(uuid.uuid4()),
        "name": clean_company_name(name),   # 入庫鎖喉點：股號後綴一律清掉
        "tax_id": tax_id,
        "labels": [label] if label else [],
        "industries": inds,
        "group": "",
        "listing_status": "非公發",
        "capital": 0,
        "authorized_capital": 0,
        "representative": "",
        "par_value": 0,
        "no_par_value": False,
        "total_shares": 0,
        "directors": [],
        "address": "",
        "setup_date": "",
        "last_change_date": "",
        "register_org": "",
        "blurb": "",
        "summary": "",
        "watched": False,
        "call_memo": {},
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    return upsert_company(company)


def add_label_to_company(company_id: str, label: str) -> dict | None:
    with _LOCK:
        company = get_company(company_id)
        if company is None:
            return None
        if label and label not in company["labels"]:
            company["labels"].append(label)
        company["last_updated"] = datetime.now(timezone.utc).isoformat()
        return upsert_company(company)


def update_company(company_id: str, updates: dict) -> dict | None:
    with _LOCK:
        company = get_company(company_id)
        if company is None:
            return None
        company.update(updates)
        company["last_updated"] = datetime.now(timezone.utc).isoformat()
        return upsert_company(company)


def update_companies_industry(id_to_industry: dict[str, str]) -> int:
    """Add an industry to many companies in a single locked write (ADD, not replace)."""
    if not id_to_industry:
        return 0
    with _LOCK:
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        store["companies"], _ = _ensure_industries_field(store["companies"])
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for c in store["companies"]:
            if c["id"] in id_to_industry:
                ind = id_to_industry[c["id"]]
                if ind and ind not in c["industries"]:
                    c["industries"].append(ind)
                    c["last_updated"] = now
                    count += 1
        _write(COMPANIES_FILE, store)
        return count


def update_companies_fields(id_to_fields: dict[str, dict]) -> int:
    """Apply per-company field updates in a single locked atomic write (avoids N full
    file rewrites when many companies change at once, e.g. competitor back-linking)."""
    if not id_to_fields:
        return 0
    with _LOCK:
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for c in store["companies"]:
            fields = id_to_fields.get(c.get("id"))
            if fields:
                c.update(fields)
                c["last_updated"] = now
                count += 1
        _write(COMPANIES_FILE, store)
        return count


def remove_companies_industry(id_to_industry: dict[str, str]) -> int:
    """Remove an industry from many companies in a single locked write."""
    if not id_to_industry:
        return 0
    with _LOCK:
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        store["companies"], _ = _ensure_industries_field(store["companies"])
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for c in store["companies"]:
            if c["id"] in id_to_industry:
                ind = id_to_industry[c["id"]]
                if ind in c["industries"]:
                    c["industries"].remove(ind)
                    c["last_updated"] = now
                    count += 1
        _write(COMPANIES_FILE, store)
        return count


def delete_company(company_id: str) -> bool:
    with _LOCK:
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        before = len(store["companies"])
        store["companies"] = [c for c in store["companies"] if c.get("id") != company_id]
        if len(store["companies"]) < before:
            _write(COMPANIES_FILE, store)
            return True
        return False


# --- Config ---

def get_config() -> dict:
    return _read(CONFIG_FILE, DEFAULT_CONFIG)


def save_ai_engine(engine: str) -> str:
    with _LOCK:
        config = get_config()
        config["ai_engine"] = engine
        _write(CONFIG_FILE, config)
        return engine


def get_industries() -> list[str]:
    return get_config()["industries"]


def get_industry_tree() -> dict[str, list[str]]:
    return get_config().get("industry_tree", {})


def save_industry_tree(tree: dict[str, list[str]]) -> dict[str, list[str]]:
    with _LOCK:
        config = get_config()
        config["industry_tree"] = tree
        _write(CONFIG_FILE, config)
        return tree


def add_industry(name: str) -> list[str]:
    with _LOCK:
        config = get_config()
        if name not in config["industries"]:
            config["industries"].append(name)
            _write(CONFIG_FILE, config)
        return config["industries"]


def rename_industry(old_name: str, new_name: str) -> list[str]:
    with _LOCK:
        config = get_config()
        if old_name in config["industries"]:
            config["industries"] = [new_name if i == old_name else i for i in config["industries"]]
            # Keep tree in sync
            tree = config.get("industry_tree", {})
            config["industry_tree"] = {
                (new_name if k == old_name else k): [new_name if c == old_name else c for c in v]
                for k, v in tree.items()
            }
            _write(CONFIG_FILE, config)
            store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
            store["companies"], _ = _ensure_industries_field(store["companies"])
            for c in store["companies"]:
                c["industries"] = [new_name if i == old_name else i for i in c["industries"]]
            _write(COMPANIES_FILE, store)
        return config["industries"]


def delete_industry(name: str) -> list[str]:
    with _LOCK:
        config = get_config()
        config["industries"] = [i for i in config["industries"] if i != name]
        # Keep tree in sync: remove as parent and as child
        tree = config.get("industry_tree", {})
        config["industry_tree"] = {
            k: [c for c in v if c != name]
            for k, v in tree.items()
            if k != name
        }
        _write(CONFIG_FILE, config)
        return config["industries"]


def apply_subdivision(parent: str, groups: list[dict]) -> dict:
    """把一個產業細分成子產業（產業地圖 Phase 2）。在鎖內各一次原子寫完成 config 與
    companies，避免中途壞檔：
      - 每個 group 的 name 加進 industries（若新）、掛到 industry_tree[parent] 底下
      - 每個 group 的 company_ids：把 parent 標籤換成該 child 標籤（移除父、加上子）
    `groups`：[{"name": 子產業名, "company_ids": [...]}]。回傳異動摘要。"""
    id_to_child: dict[str, str] = {}
    for g in groups:
        ch = (g.get("name") or "").strip()
        if not ch:
            continue
        for cid in g.get("company_ids", []):
            id_to_child[cid] = ch

    with _LOCK:
        # 1) config：新增子產業 + 掛進樹
        config = get_config()
        industries = config["industries"]
        tree = config.get("industry_tree", {})
        added_children: list[str] = []
        existing_kids = list(tree.get(parent, []))
        for g in groups:
            ch = (g.get("name") or "").strip()
            if not ch:
                continue
            if ch not in industries:
                industries.append(ch)
            if ch not in existing_kids:
                existing_kids.append(ch)
                added_children.append(ch)
        tree[parent] = existing_kids
        config["industry_tree"] = tree
        _write(CONFIG_FILE, config)

        # 2) companies：把 parent 標籤換成對應 child 標籤
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        store["companies"], _ = _ensure_industries_field(store["companies"])
        now = datetime.now(timezone.utc).isoformat()
        retagged = 0
        for c in store["companies"]:
            ch = id_to_child.get(c["id"])
            if not ch:
                continue
            inds = c["industries"]
            changed = False
            if parent in inds:
                inds.remove(parent)
                changed = True
            if ch not in inds:
                inds.append(ch)
                changed = True
            if changed:
                c["last_updated"] = now
                retagged += 1
        _write(COMPANIES_FILE, store)

    return {"added_children": added_children, "retagged": retagged}


def merge_children_into(parent: str, descendants: list[str]) -> dict:
    """apply_subdivision 的逆操作：把 parent 底下的子產業（descendants，含各層後代）
    合併回 parent。掛在任一 descendant 的公司改掛回 parent；descendants 從 industries
    與 industry_tree 中移除。之後 parent 沒有子產業 → 產業地圖會回到葉模式（單張完整
    地圖，含競業候選）。在鎖內各一次原子寫完成。"""
    desc_set = set(descendants)
    with _LOCK:
        # companies：任何掛在 descendant 的公司 → 移除該標籤、加回 parent
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        store["companies"], _ = _ensure_industries_field(store["companies"])
        now = datetime.now(timezone.utc).isoformat()
        retagged = 0
        for c in store["companies"]:
            inds = c["industries"]
            hit = [i for i in inds if i in desc_set]
            if not hit:
                continue
            for i in hit:
                inds.remove(i)
            if parent not in inds:
                inds.append(parent)
            c["last_updated"] = now
            retagged += 1
        _write(COMPANIES_FILE, store)

        # config：移除 descendants（industries + tree 中的 key 與被引用處）
        config = get_config()
        config["industries"] = [i for i in config["industries"] if i not in desc_set]
        tree = config.get("industry_tree", {})
        new_tree: dict[str, list[str]] = {}
        for k, v in tree.items():
            if k in desc_set:
                continue
            kids = [c for c in v if c not in desc_set]
            if kids:
                new_tree[k] = kids
        config["industry_tree"] = new_tree
        _write(CONFIG_FILE, config)

    return {"removed": list(desc_set), "retagged": retagged}


def reconcile_industries() -> dict:
    """啟動對帳（非破壞）：把公司掛著、但 config.industries 已無的產業補回 config。
    rename/delete/subdivision/merge 是 config+companies 兩段寫，中途被 kill 會留下
    『公司有標籤但選單看不到、無法選取/清除』的殭屍標籤——補回 config 即可讓它重新可管理。"""
    with _LOCK:
        config = get_config()
        industries = config.get("industries", [])
        valid = set(industries)
        companies = _read(COMPANIES_FILE, DEFAULT_COMPANIES)["companies"]
        companies, _ = _ensure_industries_field(companies)
        readded: list[str] = []
        for c in companies:
            for ind in c.get("industries", []):
                if ind and ind not in valid:
                    valid.add(ind)
                    industries.append(ind)
                    readded.append(ind)
        if readded:
            config["industries"] = industries
            _write(CONFIG_FILE, config)
        return {"readded_industries": readded}


def add_label(label: str) -> None:
    with _LOCK:
        config = get_config()
        if label and label not in config["labels"]:
            config["labels"].append(label)
            _write(CONFIG_FILE, config)


# --- Label groups ---

def get_label_groups() -> dict[str, list[str]]:
    return get_config().get("label_groups", {})


def save_label_group(name: str, labels: list[str]) -> dict[str, list[str]]:
    with _LOCK:
        config = get_config()
        groups = config.get("label_groups", {})
        groups[name] = labels
        config["label_groups"] = groups
        _write(CONFIG_FILE, config)
        return groups


def delete_label_group(name: str) -> dict[str, list[str]]:
    with _LOCK:
        config = get_config()
        groups = config.get("label_groups", {})
        groups.pop(name, None)
        config["label_groups"] = groups
        _write(CONFIG_FILE, config)
        return groups


# --- Industry keywords (for daily news synonym expansion) ---

def get_all_industry_keywords() -> dict[str, list[str]]:
    """Return {industry: [keyword, ...]} from persistent storage."""
    if not KEYWORDS_FILE.exists():
        return {}
    try:
        return json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_keywords_for_industry(industry: str) -> list[str]:
    return get_all_industry_keywords().get(industry, [])


def save_industry_keywords(industry: str, keywords: list[str]) -> None:
    with _LOCK:
        store = get_all_industry_keywords()
        store[industry] = keywords
        KEYWORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _write(KEYWORDS_FILE, store)
