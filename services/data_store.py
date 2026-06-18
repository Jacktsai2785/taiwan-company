import json
import os
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


def _read(path: Path, default: dict) -> dict:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    """原子寫：先寫同目錄的 .tmp 再 os.replace（POSIX 保證 rename 原子）。
    讀者永遠看到完整的舊檔或完整的新檔，不會讀到寫一半的壞 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


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


def get_company(company_id: str) -> dict | None:
    return next((c for c in get_all_companies() if c.get("id") == company_id), None)


def find_company_by_name(name: str) -> dict | None:
    return next((c for c in get_all_companies() if c.get("name") == name), None)


def normalize_company_name(name: str) -> str:
    """Strip company-type suffix to match short and full names interchangeably."""
    n = (name or "").strip()
    for sfx in ("股份有限公司", "有限公司"):
        if n.endswith(sfx):
            return n[: -len(sfx)]
    return n


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
        "name": name,
        "tax_id": tax_id,
        "labels": [label] if label else [],
        "industries": inds,
        "group": "",
        "listing_status": "非公發",
        "capital": 0,
        "authorized_capital": 0,
        "representative": "",
        "par_value": 0,
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
