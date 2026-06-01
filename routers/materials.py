"""
Company materials (簡報 / 介紹 / 照片) upload + AI profile generation.

Files are persisted under data/uploads/{company_id}/ and served via the
/uploads static mount so the user can click to view the originals. A separate
latest-Opus pass scans all uploaded files and writes a standalone profile into the
company's `materials_summary` field (kept separate from the public-data
`summary`).
"""
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from services import data_store, report_generator
from services.ai_deps import ai_from_headers
from services.file_parser import extract_text

router = APIRouter(prefix="/api/companies", tags=["materials"])

UPLOADS_DIR = data_store.DATA_DIR / "uploads"

# Files Claude reads natively (passed as paths / vision blocks)
_NATIVE_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"}
# Files we pre-extract to text (office docs + OCR-only images)
_TEXT_EXTS = {".pptx", ".docx", ".xlsx", ".xls", ".txt", ".tiff", ".tif", ".bmp"}
_ACCEPTED_EXTS = _NATIVE_EXTS | _TEXT_EXTS
_MAX_BYTES = 30 * 1024 * 1024  # 30 MB per file


def _safe_name(filename: str) -> str:
    """Strip any path component and reduce to a filesystem-safe basename."""
    base = Path(filename or "file").name
    base = re.sub(r"[^\w.\-]", "_", base, flags=re.UNICODE)
    return base or "file"


def _company_dir(company_id: str) -> Path:
    d = UPLOADS_DIR / company_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _require_company(company_id: str) -> dict:
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/{company_id}/materials")
def list_materials(company_id: str):
    company = _require_company(company_id)
    return {
        "materials": company.get("materials") or [],
        "materials_summary": company.get("materials_summary") or "",
        "materials_blurb": company.get("materials_blurb") or "",
        "materials_generated_at": company.get("materials_generated_at") or "",
    }


@router.post("/{company_id}/materials")
async def upload_materials(company_id: str, files: list[UploadFile] = File(...)):
    company = _require_company(company_id)
    base_dir = _company_dir(company_id)
    materials: list[dict] = list(company.get("materials") or [])
    existing_names = {m["stored_name"] for m in materials}
    saved: list[dict] = []

    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in _ACCEPTED_EXTS:
            raise HTTPException(status_code=422, detail=f"不支援的檔案格式：{ext or '（無副檔名）'}")
        content = await f.read()
        if len(content) > _MAX_BYTES:
            raise HTTPException(status_code=422, detail=f"檔案過大（>30MB）：{f.filename}")

        stored = _safe_name(f.filename or f"file{ext}")
        # de-dup stored filename
        stem, suffix = Path(stored).stem, Path(stored).suffix
        i = 1
        while stored in existing_names or (base_dir / stored).exists():
            stored = f"{stem}_{i}{suffix}"
            i += 1
        (base_dir / stored).write_bytes(content)
        existing_names.add(stored)

        entry = {
            "filename": Path(f.filename or stored).name,
            "stored_name": stored,
            "url": f"/uploads/{company_id}/{stored}",
            "mime_type": f.content_type or mimetypes.guess_type(stored)[0] or "application/octet-stream",
            "size": len(content),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        materials.append(entry)
        saved.append(entry)

    data_store.update_company(company_id, {"materials": materials})
    return {"materials": materials, "saved": saved}


@router.delete("/{company_id}/materials/{stored_name}")
def delete_material(company_id: str, stored_name: str):
    company = _require_company(company_id)
    materials = list(company.get("materials") or [])
    stored_name = Path(stored_name).name  # prevent path traversal
    remaining = [m for m in materials if m.get("stored_name") != stored_name]
    if len(remaining) == len(materials):
        raise HTTPException(status_code=404, detail="檔案不存在")

    fpath = _company_dir(company_id) / stored_name
    try:
        fpath.unlink(missing_ok=True)
    except OSError:
        pass

    data_store.update_company(company_id, {"materials": remaining})
    return {"materials": remaining}


@router.post("/{company_id}/materials/generate")
async def generate_from_materials(company_id: str, ai: dict = Depends(ai_from_headers)):
    company = _require_company(company_id)
    materials = company.get("materials") or []
    interview_text = report_generator.serialize_memo(company.get("call_memo"))
    if not materials and not interview_text.strip():
        raise HTTPException(status_code=422, detail="尚無補充資料（請上傳檔案或填寫訪談備忘錄）")

    base_dir = _company_dir(company_id)
    native_paths: list[str] = []
    text_parts: list[str] = []

    for m in materials:
        stored = m.get("stored_name", "")
        path = base_dir / stored
        if not path.exists():
            continue
        ext = Path(stored).suffix.lower()
        if ext in _NATIVE_EXTS:
            native_paths.append(str(path))
        else:
            try:
                if ext == ".txt":
                    txt = path.read_bytes().decode("utf-8", errors="replace")
                else:
                    txt = extract_text(m.get("filename", stored), path.read_bytes())
            except Exception:
                txt = ""
            if txt and not txt.startswith("["):
                text_parts.append(f"── 檔案：{m.get('filename', stored)} ──\n{txt}")

    if not native_paths and not text_parts and not interview_text.strip():
        raise HTTPException(status_code=422, detail="無法從補充資料讀取任何內容")

    materials_text = "\n\n".join(text_parts)
    result = await report_generator.generate_summary_from_materials(
        company, native_paths, materials_text, interview_text, **ai
    )

    now = datetime.now(timezone.utc).isoformat()
    fields = {
        "materials_summary": result.get("summary", ""),
        "materials_blurb": result.get("blurb", ""),
        "materials_generated_at": now,
    }
    data_store.update_company(company_id, fields)
    return fields


# ── Section-level merge into the public 公司簡介 (summary) ─────────────────────

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")

# Public DD sections (from registry/web) live at the top level. Deck-only topics
# are grouped under one collapsible umbrella so they read as "公司概況 的延伸".
PUBLIC_SECTIONS = ["業務概況", "競業分析", "主要風險"]
UMBRELLA = "營運綜覽"
# Top-level reading order: the deck umbrella sits right under 業務概況.
_TOP_ORDER = ["業務概況", UMBRELLA, "競業分析", "主要風險"]
# Order of deck topics inside the umbrella; unknown headings keep their order at end.
_SUB_ORDER = ["產品與服務", "商業模式與市場", "團隊與股東", "財務與募資亮點", "投資亮點"]

_SUBHEADING_RE = re.compile(r"^###\s+(.+?)\s*$")


def _parse_subsections(body: str) -> list[dict]:
    """Split umbrella body into `### heading` sub-sections."""
    subs: list[dict] = []
    cur: dict | None = None
    for line in (body or "").split("\n"):
        m = _SUBHEADING_RE.match(line.strip())
        if m:
            cur = {"heading": m.group(1).strip(), "body": []}
            subs.append(cur)
        elif cur is not None:
            cur["body"].append(line)
    for s in subs:
        s["body"] = "\n".join(s["body"]).strip("\n")
    return subs


def _normalize_to_umbrella(sections: list[dict], valid_subs: set[str] | None = None) -> list[dict]:
    """Reshape a flat section list so public DD sections stay top-level and every
    other (deck) section is collected as a `### sub-section` under the UMBRELLA.
    Idempotent: an existing UMBRELLA is unpacked and rebuilt.

    If `valid_subs` is given, umbrella sub-sections whose heading isn't in it are
    dropped — used on apply to purge stale subs the latest deck no longer produces
    (e.g. a renamed section)."""
    public: list[dict] = []
    subs: list[dict] = []
    for s in sections:
        if s["heading"] in PUBLIC_SECTIONS:
            public.append(s)
        elif s["heading"] == UMBRELLA:
            subs.extend(_parse_subsections(s["body"]))
        else:
            subs.append(s)  # stray top-level deck section → fold into umbrella

    # de-dup sub-sections by heading (last write wins), then order
    by_sub: dict[str, dict] = {}
    for s in subs:
        by_sub[s["heading"]] = s
    if valid_subs is not None:
        by_sub = {h: s for h, s in by_sub.items() if h in valid_subs}
    ordered_subs = sorted(
        by_sub.values(),
        key=lambda s: (_SUB_ORDER.index(s["heading"]) if s["heading"] in _SUB_ORDER else len(_SUB_ORDER)),
    )
    result = list(public)
    if ordered_subs:
        umb_body = "\n\n".join(f"### {s['heading']}\n{s['body']}".rstrip() for s in ordered_subs)
        result.append({"heading": UMBRELLA, "body": umb_body})
    # Order top-level sections so 營運綜覽 sits right under 業務概況.
    result.sort(key=lambda s: _TOP_ORDER.index(s["heading"]) if s["heading"] in _TOP_ORDER else len(_TOP_ORDER))
    return result


def _parse_sections(md: str) -> list[dict]:
    """Split a Markdown summary into ordered sections by `## heading`.
    Returns [{heading, body}]. Any preamble before the first `##` is dropped."""
    sections: list[dict] = []
    cur: dict | None = None
    for line in (md or "").split("\n"):
        m = _HEADING_RE.match(line.strip())
        if m:
            cur = {"heading": m.group(1).strip(), "body": []}
            sections.append(cur)
        elif cur is not None:
            cur["body"].append(line)
    for s in sections:
        s["body"] = "\n".join(s["body"]).strip("\n")
    return sections


def _serialize_sections(sections: list[dict]) -> str:
    parts = []
    for s in sections:
        body = s["body"].strip("\n")
        parts.append(f"## {s['heading']}\n{body}".rstrip())
    return "\n\n".join(parts).strip() + "\n"


class ApplyRequest(BaseModel):
    headings: list[str]  # which materials-summary sections to apply


@router.post("/{company_id}/materials/apply")
def apply_materials(company_id: str, req: ApplyRequest):
    """Merge selected sections of `materials_summary` into the public `summary`.

    A deck section whose heading matches a public DD section (業務概況/競業分析/
    主要風險) replaces that section's body in place (修改); every other deck topic
    is grouped as a `### sub-section` under the「營運綜覽」umbrella (歸入綜覽).
    `materials_applied_headings` records the top-level sections carrying deck
    content so the modal can colour-mark them with a「簡報」chip."""
    company = _require_company(company_id)
    mat_summary = company.get("materials_summary") or ""
    if not mat_summary.strip():
        raise HTTPException(status_code=422, detail="尚未生成簡報簡介，請先生成")

    wanted = set(req.headings or [])
    mat_sections = [s for s in _parse_sections(mat_summary) if s["heading"] in wanted]
    if not mat_sections:
        raise HTTPException(status_code=422, detail="未選取任何段落")

    base_sections = _parse_sections(company.get("summary") or "")
    by_heading = {s["heading"]: s for s in base_sections}

    for ms in mat_sections:
        h = ms["heading"]
        if h in by_heading:
            by_heading[h]["body"] = ms["body"]          # 取代現有段落（含 public 業務概況）
        else:
            base_sections.append({"heading": h, "body": ms["body"]})  # 暫置頂層，下面收進綜覽

    # Purge stale umbrella subs the latest generation no longer produces (e.g. a
    # renamed section): keep only sub-headings present in the current deck output.
    current_deck_subs = {s["heading"] for s in _parse_sections(mat_summary)} - set(PUBLIC_SECTIONS)
    final_sections = _normalize_to_umbrella(base_sections, valid_subs=current_deck_subs)

    # Top-level sections carrying deck content get the「簡報」chip: the umbrella,
    # plus any public section whose body the deck replaced. Keep prior marks too.
    mat_headings = {s["heading"] for s in mat_sections}
    applied = set(company.get("materials_applied_headings") or [])
    applied |= {h for h in mat_headings if h in PUBLIC_SECTIONS}
    if any(s["heading"] == UMBRELLA for s in final_sections):
        applied.add(UMBRELLA)
    applied &= {s["heading"] for s in final_sections}  # only keep marks that still exist

    merged = _serialize_sections(final_sections) if final_sections else ""
    fields = {
        "summary": merged,
        "materials_applied_headings": sorted(applied),
    }
    data_store.update_company(company_id, fields)
    return fields
