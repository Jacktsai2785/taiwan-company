import asyncio
import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import create_model

from services import claude_client, data_store, memo_extractor
from services.ai_deps import ai_from_headers
from services.file_parser import extract_text, FileParseError
from services import whisper_transcriber

router = APIRouter(prefix="/api/companies", tags=["call_memo"])

_MAX_BYTES = 30 * 1024 * 1024        # 逐字稿文件 30MB（與 upload 一致）
_AUDIO_MAX_BYTES = 100 * 1024 * 1024  # 音檔放寬到 100MB
_MEMO_SOURCES_DIR = data_store.DATA_DIR / "uploads"
_MEMO_RUNS_DIR = data_store.DATA_DIR / "memo_runs"

# 單一來源：欄位定義只在 memo_extractor.FIELDS 維護，MemoSave 由它 + interview_date/label 動態生成
MemoSave = create_model(
    "MemoSave",
    interview_date=(str, ""),
    label=(str, ""),
    **{key: (str, "") for key in memo_extractor.FIELD_KEYS},
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_memo_entry() -> dict:
    now = _now()
    entry = {
        "id": str(uuid.uuid4()),
        "label": "",
        "interview_date": "",
        "created_at": now,
        "updated_at": now,
    }
    entry.update({key: "" for key in memo_extractor.FIELD_KEYS})
    return entry


def _migrated_memos_from_legacy(company: dict) -> list[dict]:
    """一次性把舊版單一 call_memo 物件包成新版陣列的第一筆，資料不遺失。"""
    legacy = company.get("call_memo") or {}
    if not legacy:
        return []
    entry = _new_memo_entry()
    entry.update({k: legacy.get(k, "") for k in memo_extractor.FIELD_KEYS})
    entry["interview_date"] = legacy.get("interview_date", "")
    if company.get("call_memo_source"):
        entry["source"] = company["call_memo_source"]
    if company.get("call_memo_runs"):
        entry["runs"] = company["call_memo_runs"]
    if company.get("call_memo_last_run"):
        entry["last_run"] = company["call_memo_last_run"]
    return [entry]


def _get_company_or_404(company_id: str) -> dict:
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _get_memos(company_id: str, company: dict | None = None, persist_migration: bool = True) -> list[dict]:
    company = company or _get_company_or_404(company_id)
    memos = company.get("call_memos")
    if memos is not None:
        return memos
    migrated = _migrated_memos_from_legacy(company)
    if migrated and persist_migration:
        data_store.update_company(company_id, {"call_memos": migrated})
    return migrated


def _find_memo(memos: list[dict], memo_id: str) -> dict:
    memo = next((m for m in memos if m.get("id") == memo_id), None)
    if memo is None:
        raise HTTPException(status_code=404, detail="Call memo not found")
    return memo


def _save_memos(company_id: str, memos: list[dict]) -> list[dict]:
    company = data_store.update_company(company_id, {"call_memos": memos})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company.get("call_memos") or []


async def _extract_text_content(filename: str, content: bytes) -> str:
    if Path(filename).suffix.lower() in {".txt", ".md"}:
        return content.decode("utf-8", errors="replace")
    try:
        return await asyncio.to_thread(extract_text, filename, content)
    except FileParseError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _write_memo_source_file(company_id: str, filename: str, content: bytes) -> dict:
    if not company_id or Path(company_id).name != company_id:
        raise HTTPException(status_code=400, detail="Invalid company_id")
    suffix = Path(filename).suffix.lower()
    stored_name = f"memo_source_{uuid.uuid4().hex}{suffix}"
    company_dir = _MEMO_SOURCES_DIR / company_id
    company_dir.mkdir(parents=True, exist_ok=True)
    (company_dir / stored_name).write_bytes(content)
    return {
        "filename": Path(filename).name,
        "stored_name": stored_name,
        "url": f"/uploads/{company_id}/{stored_name}",
        "size": len(content),
        "uploaded_at": _now(),
    }


def _memo_source_path(company_id: str, source: dict) -> Path:
    stored_name = Path(source.get("stored_name") or "").name
    if not stored_name:
        raise HTTPException(status_code=404, detail="尚未保存逐字稿來源")
    path = _MEMO_SOURCES_DIR / company_id / stored_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="逐字稿原檔不存在，請重新上傳")
    return path


def _record_memo_run(
    company_id: str,
    memo_id: str,
    fields: dict,
    engine: str,
    filename: str,
    content: bytes,
    transcript: str,
    audit: dict,
) -> dict:
    """把抽取結果 + 執行紀錄寫回指定的那份 call memo（而不是整間公司唯一一份）。"""
    prepared = memo_extractor.prepare_transcript(transcript, filename)
    run_id = str(uuid.uuid4())
    audit_dir = _MEMO_RUNS_DIR / company_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / f"{run_id}.json"
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    run = {
        "id": run_id,
        "created_at": _now(),
        "source_filename": Path(filename).name,
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "engine": engine,
        "model": claude_client.model_for_engine(engine),
        "prompt_version": memo_extractor.PROMPT_VERSION,
        "chunk_count": len(memo_extractor._split_transcript(prepared)),
        "evidence_file": str(audit_path.relative_to(data_store.DATA_DIR)),
        "coverage": audit.get("coverage") or {},
        "fields": fields,
    }

    memos = _get_memos(company_id)
    memo = _find_memo(memos, memo_id)
    memo.update(fields)
    runs = list(memo.get("runs") or [])
    runs.append(run)
    memo["runs"] = runs[-20:]
    memo["last_run"] = run
    memo["updated_at"] = _now()
    _save_memos(company_id, memos)
    return memo


@router.get("/{company_id}/memos")
def list_memos(company_id: str):
    return _get_memos(company_id)


@router.post("/{company_id}/memos")
def create_memo(company_id: str):
    memos = _get_memos(company_id)
    entry = _new_memo_entry()
    memos.append(entry)
    _save_memos(company_id, memos)
    return entry


@router.get("/{company_id}/memos/{memo_id}")
def get_memo(company_id: str, memo_id: str):
    return _find_memo(_get_memos(company_id), memo_id)


@router.put("/{company_id}/memos/{memo_id}")
def save_memo(company_id: str, memo_id: str, memo: MemoSave):
    memos = _get_memos(company_id)
    entry = _find_memo(memos, memo_id)
    entry.update(memo.model_dump())
    entry["id"] = memo_id
    entry["updated_at"] = _now()
    _save_memos(company_id, memos)
    return entry


@router.delete("/{company_id}/memos/{memo_id}")
def delete_memo(company_id: str, memo_id: str):
    memos = _get_memos(company_id)
    entry = _find_memo(memos, memo_id)
    remaining = [m for m in memos if m.get("id") != memo_id]

    source = entry.get("source") or {}
    stored_name = Path(source.get("stored_name") or "").name
    if stored_name:
        try:
            (_MEMO_SOURCES_DIR / company_id / stored_name).unlink(missing_ok=True)
        except OSError:
            pass
    for run in entry.get("runs") or []:
        evidence_file = run.get("evidence_file")
        if evidence_file:
            try:
                (data_store.DATA_DIR / evidence_file).unlink(missing_ok=True)
            except OSError:
                pass

    return {"call_memos": _save_memos(company_id, remaining)}


@router.get("/{company_id}/memos/{memo_id}/source")
def get_memo_source(company_id: str, memo_id: str):
    memo = _find_memo(_get_memos(company_id), memo_id)
    return memo.get("source") or {}


@router.post("/{company_id}/memos/{memo_id}/extract")
async def extract_memo(company_id: str, memo_id: str, file: UploadFile = File(...), ai: dict = Depends(ai_from_headers)):
    company = _get_company_or_404(company_id)
    _find_memo(_get_memos(company_id, company), memo_id)  # 404 early if memo_id is invalid

    content = await file.read()
    if len(content) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="逐字稿檔過大，上限 30MB")
    filename = file.filename or "transcript.txt"

    transcript = await _extract_text_content(filename, content)
    if not transcript.strip():
        raise HTTPException(status_code=422, detail="無法從檔案中取得文字內容")

    source = _write_memo_source_file(company_id, filename, content)

    try:
        fields, audit = await memo_extractor.extract_with_audit(
            company["name"], transcript, source_filename=filename, **ai
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    memos = _get_memos(company_id)
    memo = _find_memo(memos, memo_id)
    memo["source"] = source
    _save_memos(company_id, memos)
    _record_memo_run(company_id, memo_id, fields, ai["engine"], filename, content, transcript, audit)
    return fields


@router.post("/{company_id}/memos/{memo_id}/reextract")
async def reextract_memo(company_id: str, memo_id: str, ai: dict = Depends(ai_from_headers)):
    company = _get_company_or_404(company_id)
    memo = _find_memo(_get_memos(company_id, company), memo_id)
    source = memo.get("source") or {}
    path = _memo_source_path(company_id, source)
    content = await asyncio.to_thread(path.read_bytes)
    filename = source.get("filename") or path.name
    transcript = await _extract_text_content(filename, content)
    if not transcript.strip():
        raise HTTPException(status_code=422, detail="無法從保存的逐字稿中取得文字內容")
    try:
        fields, audit = await memo_extractor.extract_with_audit(
            company["name"], transcript, source_filename=filename, **ai
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _record_memo_run(company_id, memo_id, fields, ai["engine"], filename, content, transcript, audit)
    return fields


@router.post("/{company_id}/memos/{memo_id}/transcribe-audio")
async def transcribe_audio_memo(
    company_id: str,
    memo_id: str,
    file: UploadFile = File(...),
    ai: dict = Depends(ai_from_headers),
):
    company = _get_company_or_404(company_id)
    _find_memo(_get_memos(company_id, company), memo_id)

    suffix = Path(file.filename or "audio.mp3").suffix.lower()
    if suffix not in whisper_transcriber.SUPPORTED_EXTS:
        raise HTTPException(status_code=422, detail=f"不支援的音訊格式：{suffix}，請上傳 MP3 / WAV / M4A / OGG / WEBM / FLAC")

    content = await file.read()
    if len(content) > _AUDIO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="音檔過大，上限 100MB，請壓縮或分段上傳")
    try:
        transcript = await whisper_transcriber.transcribe_audio(content, suffix)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if not transcript.strip():
        raise HTTPException(status_code=422, detail="無法辨識音訊內容，請確認檔案包含清晰語音")

    try:
        fields, audit = await memo_extractor.extract_with_audit(
            company["name"], transcript, source_filename=file.filename or "", **ai
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _record_memo_run(
        company_id,
        memo_id,
        fields,
        ai["engine"],
        file.filename or f"audio{suffix}",
        content,
        transcript,
        audit,
    )
    return {"transcript": transcript, "fields": fields}


@router.get("/{company_id}/memos/{memo_id}/download")
def download_memo(company_id: str, memo_id: str):
    company = _get_company_or_404(company_id)
    memo = _find_memo(_get_memos(company_id, company), memo_id)
    interview_date = memo.get("interview_date") or date.today().strftime("%Y/%m/%d")

    docx_bytes = memo_extractor.fill_template(company, memo, interview_date)

    safe_name = company["name"].replace("/", "-").replace("\\", "-")
    filename = f"{safe_name}callmemo_{interview_date.replace('/', '')}.docx"
    encoded_filename = quote(filename, safe="")

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        # Starlette headers must be Latin-1 encodable. RFC 5987 filename* keeps
        # Chinese company names without putting raw Unicode in the header.
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )
