import json
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


def _read(path: Path, default: dict) -> dict:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Companies ---

def get_all_companies() -> list[dict]:
    return _read(COMPANIES_FILE, DEFAULT_COMPANIES)["companies"]


def get_company(company_id: str) -> dict | None:
    return next((c for c in get_all_companies() if c["id"] == company_id), None)


def find_company_by_name(name: str) -> dict | None:
    return next((c for c in get_all_companies() if c["name"] == name), None)


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
                (c for c in companies if normalize_company_name(c["name"]) == target),
                None,
            )
    return None




def upsert_company(company: dict) -> dict:
    store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
    companies = store["companies"]
    idx = next((i for i, c in enumerate(companies) if c["id"] == company["id"]), None)
    if idx is not None:
        companies[idx] = company
    else:
        companies.append(company)
    _write(COMPANIES_FILE, store)
    return company


def create_company(name: str, label: str, industry: str, tax_id: str = "") -> dict:
    company = {
        "id": str(uuid.uuid4()),
        "name": name,
        "tax_id": tax_id,
        "labels": [label] if label else [],
        "industry": industry,
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
    company = get_company(company_id)
    if company is None:
        return None
    if label and label not in company["labels"]:
        company["labels"].append(label)
    company["last_updated"] = datetime.now(timezone.utc).isoformat()
    return upsert_company(company)


def update_company(company_id: str, updates: dict) -> dict | None:
    company = get_company(company_id)
    if company is None:
        return None
    company.update(updates)
    company["last_updated"] = datetime.now(timezone.utc).isoformat()
    return upsert_company(company)


def update_companies_industry(id_to_industry: dict[str, str]) -> int:
    """Apply industry updates to many companies atomically (one read + one write)."""
    if not id_to_industry:
        return 0
    store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for c in store["companies"]:
        if c["id"] in id_to_industry:
            c["industry"] = id_to_industry[c["id"]]
            c["last_updated"] = now
            count += 1
    _write(COMPANIES_FILE, store)
    return count


def delete_company(company_id: str) -> bool:
    store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
    before = len(store["companies"])
    store["companies"] = [c for c in store["companies"] if c["id"] != company_id]
    if len(store["companies"]) < before:
        _write(COMPANIES_FILE, store)
        return True
    return False


# --- Config ---

def get_config() -> dict:
    return _read(CONFIG_FILE, DEFAULT_CONFIG)


def get_industries() -> list[str]:
    return get_config()["industries"]


def add_industry(name: str) -> list[str]:
    config = get_config()
    if name not in config["industries"]:
        config["industries"].append(name)
        _write(CONFIG_FILE, config)
    return config["industries"]


def rename_industry(old_name: str, new_name: str) -> list[str]:
    config = get_config()
    if old_name in config["industries"]:
        config["industries"] = [new_name if i == old_name else i for i in config["industries"]]
        _write(CONFIG_FILE, config)
        # Update companies that used old industry name
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        for c in store["companies"]:
            if c.get("industry") == old_name:
                c["industry"] = new_name
        _write(COMPANIES_FILE, store)
    return config["industries"]


def delete_industry(name: str) -> list[str]:
    config = get_config()
    config["industries"] = [i for i in config["industries"] if i != name]
    _write(CONFIG_FILE, config)
    return config["industries"]


def add_label(label: str) -> None:
    config = get_config()
    if label and label not in config["labels"]:
        config["labels"].append(label)
        _write(CONFIG_FILE, config)


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
    store = get_all_industry_keywords()
    store[industry] = keywords
    KEYWORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEYWORDS_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
