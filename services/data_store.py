import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from copy import deepcopy
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


_MERGE_UNION_FIELDS = {
    "labels", "industries", "materials_applied_headings", "call_memo_runs",
}
_MERGE_CURATED_FIELDS = (
    "materials", "call_memo", "patents", "relationship_graph",
    "materials_summary", "materials_blurb", "deep_enriched_at",
)


def _company_merge_rank(company: dict) -> tuple:
    """Prefer records carrying irreplaceable user work, then completed/new data."""
    curated = sum(bool(company.get(k)) for k in _MERGE_CURATED_FIELDS)
    enriched_ok = company.get("enrich_status") == "ok"
    updated = max(
        str(company.get("deep_enriched_at") or ""),
        str(company.get("enriched_at") or ""),
        str(company.get("last_updated") or ""),
    )
    return curated, enriched_ok, updated, len(company.get("summary") or "")


def _merge_list_values(field: str, records: list[dict]) -> list:
    values: list = []
    seen: set[str] = set()

    def key(value: Any) -> str:
        if not isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        if field == "competitors":
            tax = str(value.get("tax_id") or "").strip()
            name = normalize_company_name(clean_company_name(value.get("name") or ""))
            return f"tax:{tax}" if tax else f"name:{name}"
        if field == "patents":
            return str(value.get("patent_no") or value.get("app_no") or value.get("title") or json.dumps(value, ensure_ascii=False, sort_keys=True))
        if field == "materials":
            return str(value.get("stored_name") or value.get("filename") or value.get("url") or json.dumps(value, ensure_ascii=False, sort_keys=True))
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    for record in records:
        for value in record.get(field) or []:
            marker = key(value)
            if marker not in seen:
                seen.add(marker)
                values.append(deepcopy(value))
    return values


def _merge_company_records(primary: dict, records: list[dict]) -> dict:
    """Merge duplicate legal-entity rows without discarding user-created fields.

    ``primary`` wins scalar conflicts. Empty scalar fields are backfilled from
    donors; list-like evidence is unioned; watched is true if any copy is watched.
    """
    ordered = [primary] + [r for r in records if r.get("id") != primary.get("id")]
    merged = deepcopy(primary)
    all_fields = {k for record in ordered for k in record}

    for field in all_fields:
        if field == "id":
            continue
        values = [record.get(field) for record in ordered]
        if field in _MERGE_UNION_FIELDS or any(isinstance(v, list) for v in values if v is not None):
            # Directors are a current government snapshot, not cumulative history.
            if field == "directors":
                if not merged.get(field):
                    merged[field] = deepcopy(next((v for v in values if v), []))
            else:
                merged[field] = _merge_list_values(field, ordered)
        elif field == "watched":
            merged[field] = any(v is True for v in values)
        elif any(isinstance(v, dict) for v in values if v is not None):
            combined: dict = {}
            for value in reversed(values):
                if isinstance(value, dict):
                    combined.update(deepcopy(value))
            merged[field] = combined
        elif isinstance(merged.get(field), bool):
            # bool 是 int 的子類，False == 0；但像 no_par_value=False 這種欄位，
            # False 是已確認的有效狀態，primary 已明確設定就不該被 donor 覆蓋。
            pass
        elif merged.get(field) in (None, "", 0):
            replacement = next(
                (v for v in values[1:] if isinstance(v, bool) or v not in (None, "", 0)),
                None,
            )
            if replacement is not None:
                merged[field] = deepcopy(replacement)

    donor_ids = [r["id"] for r in records if r.get("id") and r.get("id") != primary.get("id")]
    prior = [
        old_id
        for record in records
        for old_id in (record.get("merged_from_ids") or [])
        if old_id != primary.get("id")
    ]
    merged["merged_from_ids"] = list(dict.fromkeys([*prior, *donor_ids]))
    merged["merged_at"] = datetime.now(timezone.utc).isoformat()
    merged["last_updated"] = merged["merged_at"]
    return merged


def _rewrite_company_id_refs(value: Any, id_map: dict[str, str]) -> tuple[Any, int]:
    """Recursively rewrite exact IDs and /uploads/{id}/ URL path segments."""
    changed = 0
    if isinstance(value, dict):
        for key, child in list(value.items()):
            # Audit history, not a live foreign-key reference.
            if key == "merged_from_ids":
                continue
            new_child, count = _rewrite_company_id_refs(child, id_map)
            value[key] = new_child
            changed += count
        return value, changed
    if isinstance(value, list):
        for i, child in enumerate(value):
            value[i], count = _rewrite_company_id_refs(child, id_map)
            changed += count
        return value, changed
    if isinstance(value, str):
        if value in id_map:
            return id_map[value], 1
        new_value = value
        for old, new in id_map.items():
            new_value = new_value.replace(f"/uploads/{old}/", f"/uploads/{new}/")
        return new_value, int(new_value != value)
    return value, 0


def _repair_competitor_company_refs(companies: list[dict]) -> int:
    """Repair stale competitor IDs only when tax ID or normalized name is exact."""
    by_id = {c.get("id"): c for c in companies if c.get("id")}
    by_tax = {
        str(c.get("tax_id") or "").strip(): c
        for c in companies
        if str(c.get("tax_id") or "").strip()
    }
    by_name: dict[str, dict] = {}
    ambiguous_names: set[str] = set()
    for company in companies:
        name = normalize_company_name(clean_company_name(company.get("name") or ""))
        if not name:
            continue
        if name in by_name and by_name[name].get("id") != company.get("id"):
            ambiguous_names.add(name)
        else:
            by_name[name] = company

    changed = 0
    for company in companies:
        for competitor in company.get("competitors") or []:
            if not isinstance(competitor, dict):
                continue
            old_id = competitor.get("company_id")
            if not old_id or old_id in by_id:
                continue
            tax_id = str(competitor.get("tax_id") or "").strip()
            name = normalize_company_name(clean_company_name(competitor.get("name") or ""))
            match = by_tax.get(tax_id) if tax_id else None
            if match is None and name and name not in ambiguous_names:
                match = by_name.get(name)
            if match and match.get("id") != old_id:
                competitor["company_id"] = match["id"]
                changed += 1
    return changed


def _plan_upload_renames(id_map: dict[str, str]) -> dict[str, str]:
    """Predict which /uploads/ URLs _merge_upload_dirs will rename because the
    target already has a same-name file, so metadata can be rewritten to match
    before anything moves. Safe as a dry read: callers hold ``_LOCK`` for the
    whole plan-then-move sequence, so the filesystem can't change in between.
    """
    uploads = DATA_DIR / "uploads"
    renamed_urls: dict[str, str] = {}
    for old, new in id_map.items():
        source = uploads / old
        target = uploads / new
        if not source.exists() or not target.exists():
            continue
        target_names = {item.name for item in target.iterdir()}
        for item in source.iterdir():
            if item.name in target_names:
                renamed_urls[f"/uploads/{old}/{item.name}"] = (
                    f"/uploads/{new}/merged_{old[:8]}_{item.name}"
                )
    return renamed_urls


def _apply_url_renames(value: Any, renames: dict[str, str]) -> Any:
    """Recursively rewrite /uploads/ URL fragments per ``_plan_upload_renames``."""
    if not renames:
        return value
    if isinstance(value, dict):
        return {k: _apply_url_renames(v, renames) for k, v in value.items()}
    if isinstance(value, list):
        return [_apply_url_renames(v, renames) for v in value]
    if isinstance(value, str):
        for old_url, new_url in renames.items():
            if old_url in value:
                value = value.replace(old_url, new_url)
        return value
    return value


def _merge_upload_dirs(id_map: dict[str, str]) -> int:
    uploads = DATA_DIR / "uploads"
    moved = 0
    for old, new in id_map.items():
        source = uploads / old
        target = uploads / new
        if not source.exists():
            continue
        if not target.exists():
            source.replace(target)
            moved += 1
            continue
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            destination = target / item.name
            if destination.exists():
                destination = target / f"merged_{old[:8]}_{item.name}"
            shutil.move(str(item), str(destination))
            moved += 1
        source.rmdir()
    return moved


def _plan_external_company_refs(id_map: dict[str, str]) -> tuple[dict[Path, dict], int]:
    """Build external JSON rewrites without mutating the shared read cache."""
    planned: dict[Path, dict] = {}
    count = 0
    for path in DATA_DIR.glob("*.json"):
        if path == COMPANIES_FILE:
            continue
        try:
            data = deepcopy(_read(path, {}))
            data, changed = _rewrite_company_id_refs(data, id_map)
        except Exception:
            continue
        if changed:
            planned[path] = data
            count += changed
    return planned, count


def _safe_upload_dir(company_id: str) -> Path:
    """Resolve one upload directory without allowing IDs to escape DATA_DIR/uploads."""
    company_id = str(company_id or "")
    if not company_id or Path(company_id).name != company_id or company_id in {".", ".."}:
        raise ValueError(f"invalid company id for upload migration: {company_id!r}")
    return DATA_DIR / "uploads" / company_id


def _restore_file_bytes(path: Path, content: bytes | None) -> None:
    """Rollback helper using the same-directory replace guarantee as _write."""
    if content is None:
        if path.exists():
            path.unlink()
        with _CACHE_LOCK:
            _FILE_CACHE.pop(path, None)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".rollback.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_bytes(content)
        os.replace(tmp, path)
        with _CACHE_LOCK:
            _FILE_CACHE.pop(path, None)
    finally:
        if tmp.exists():
            tmp.unlink()


def _commit_company_merge_transaction(store: dict, id_map: dict[str, str]) -> dict:
    """Commit company, external-reference and upload-directory rewrites as one
    recoverable transaction. POSIX cannot atomically rename several files and
    directories together, so every touched target is snapshotted first and fully
    restored if any write/move fails.
    """
    upload_renames = _plan_upload_renames(id_map)
    external_plan, external_refs = _plan_external_company_refs(id_map)
    if upload_renames:
        store = _apply_url_renames(store, upload_renames)
        external_plan = {
            path: _apply_url_renames(data, upload_renames)
            for path, data in external_plan.items()
        }
    json_plan = {COMPANIES_FILE: store, **external_plan}
    upload_paths = {
        _safe_upload_dir(company_id)
        for pair in id_map.items()
        for company_id in pair
    }
    json_backups = {
        path: path.read_bytes() if path.exists() else None
        for path in json_plan
    }

    uploads_root = DATA_DIR / "uploads"
    uploads_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".company-merge-", dir=DATA_DIR) as tmp_name:
        backup_root = Path(tmp_name) / "uploads"
        upload_backups: dict[Path, Path | None] = {}
        for index, path in enumerate(sorted(upload_paths, key=str)):
            if path.exists():
                backup = backup_root / str(index)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(path, backup)
                upload_backups[path] = backup
            else:
                upload_backups[path] = None

        try:
            for path, data in json_plan.items():
                _write(path, data)
            upload_files_moved = _merge_upload_dirs(id_map)
        except Exception as exc:
            rollback_errors: list[str] = []
            for path, content in json_backups.items():
                try:
                    _restore_file_bytes(path, content)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{path.name}: {rollback_exc}")
            for path, backup in upload_backups.items():
                try:
                    if path.exists():
                        shutil.rmtree(path)
                    if backup is not None:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(backup, path)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    "company merge failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise

    return {
        "external_refs_rewritten": external_refs,
        "changed_files": [path.name for path in external_plan],
        "upload_files_moved": upload_files_moved,
    }


def deduplicate_companies(*, dry_run: bool = True) -> dict:
    """Collapse duplicate non-empty tax IDs and rewrite every known ID reference."""
    with _LOCK:
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        companies, _ = _ensure_industries_field(store["companies"])
        by_tax: dict[str, list[dict]] = {}
        for company in companies:
            tax_id = str(company.get("tax_id") or "").strip()
            if tax_id:
                by_tax.setdefault(tax_id, []).append(company)
        groups = {tax: rows for tax, rows in by_tax.items() if len(rows) > 1}

        plan: list[dict] = []
        id_map: dict[str, str] = {}
        merged_by_primary: dict[str, dict] = {}
        for tax_id, rows in sorted(groups.items()):
            primary = max(rows, key=_company_merge_rank)
            merged_by_primary[primary["id"]] = _merge_company_records(primary, rows)
            donors = [r["id"] for r in rows if r["id"] != primary["id"]]
            id_map.update({donor: primary["id"] for donor in donors})
            plan.append({
                "tax_id": tax_id,
                "primary_id": primary["id"],
                "primary_name": primary.get("name", ""),
                "donor_ids": donors,
                "labels": merged_by_primary[primary["id"]].get("labels", []),
                "industries": company_industries(merged_by_primary[primary["id"]]),
            })

        result = {
            "duplicate_groups": len(groups),
            "rows_before": len(companies),
            "rows_to_remove": len(id_map),
            "plan": plan,
        }
        if dry_run or not id_map:
            return result

        new_companies: list[dict] = []
        for company in companies:
            company_id = company.get("id")
            if company_id in id_map:
                continue
            new_companies.append(merged_by_primary.get(company_id, company))
        new_companies, internal_refs = _rewrite_company_id_refs(new_companies, id_map)
        stale_competitor_refs = _repair_competitor_company_refs(new_companies)
        store["companies"] = new_companies
        transaction = _commit_company_merge_transaction(store, id_map)
        result.update({
            "rows_after": len(new_companies),
            "id_map": id_map,
            "internal_refs_rewritten": internal_refs,
            "stale_competitor_refs_repaired": stale_competitor_refs,
            **transaction,
        })
        return result


def upsert_company(company: dict) -> dict:
    with _LOCK:
        store = _read(COMPANIES_FILE, DEFAULT_COMPANIES)
        companies, _ = _ensure_industries_field(store["companies"])
        idx = next((i for i, c in enumerate(companies) if c.get("id") == company["id"]), None)
        tax_id = str(company.get("tax_id") or "").strip()
        normalized_name = normalize_company_name(clean_company_name(company.get("name") or ""))

        def same_entity(candidate: dict) -> bool:
            if candidate.get("id") == company["id"]:
                return False
            candidate_tax = str(candidate.get("tax_id") or "").strip()
            if tax_id and candidate_tax:
                return tax_id == candidate_tax
            candidate_name = normalize_company_name(clean_company_name(candidate.get("name") or ""))
            return bool(normalized_name and candidate_name == normalized_name)

        collisions = [c for c in companies if same_entity(c)]
        if collisions:
            # Existing updates keep their active ID so an in-flight enrichment
            # task can continue. Brand-new inserts reuse the best existing ID.
            primary = company if idx is not None else max(collisions, key=_company_merge_rank)
            merged = _merge_company_records(primary, [primary, company, *collisions])
            id_map = {c["id"]: primary["id"] for c in [company, *collisions] if c.get("id") != primary["id"]}
            companies = [c for c in companies if c.get("id") not in id_map and c.get("id") != primary["id"]]
            companies.append(merged)
            companies, _ = _rewrite_company_id_refs(companies, id_map)
            store["companies"] = companies
            _commit_company_merge_transaction(store, id_map)
            return merged
        if idx is not None:
            companies[idx] = company
        else:
            companies.append(company)
        store["companies"] = companies
        _write(COMPANIES_FILE, store)
        return company


def create_company(name: str, label: str, industry: str = "", tax_id: str = "") -> dict:
    existing = find_company_by_name_or_tax_id(name, tax_id)
    if existing:
        updates = deepcopy(existing)
        if label and label not in updates.get("labels", []):
            updates.setdefault("labels", []).append(label)
        if industry and industry not in company_industries(updates):
            updates.setdefault("industries", company_industries(updates)).append(industry)
        return upsert_company(updates)
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
        if not any(c.get("id") == company_id for c in store["companies"]):
            return False
        if not company_id or Path(company_id).name != company_id:
            raise ValueError("Invalid company_id")

        # Hide sensitive originals immediately, but keep a rollback path until
        # the company record has been atomically committed.
        trash = DATA_DIR / ".delete-staging" / uuid.uuid4().hex
        moved: list[tuple[Path, Path]] = []
        try:
            for root_name in ("uploads", "memo_runs"):
                source = DATA_DIR / root_name / company_id
                if source.exists():
                    target = trash / root_name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, target)
                    moved.append((source, target))
            store["companies"] = [c for c in store["companies"] if c.get("id") != company_id]
            _write(COMPANIES_FILE, store)
        except Exception:
            for source, target in reversed(moved):
                if target.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, source)
            raise
        finally:
            if trash.exists() and not moved:
                shutil.rmtree(trash, ignore_errors=True)

        # The record is gone and upload URLs no longer resolve.  Remove the
        # staged originals (including memo evidence) as part of deletion.
        shutil.rmtree(trash, ignore_errors=True)
        return before != len(store["companies"])


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
