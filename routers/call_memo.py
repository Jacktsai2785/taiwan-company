import asyncio
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import create_model

from services import data_store, memo_extractor
from services.ai_deps import ai_from_headers
from services.file_parser import extract_text, FileParseError
from services import whisper_transcriber

router = APIRouter(prefix="/api/companies", tags=["call_memo"])

_MAX_BYTES = 30 * 1024 * 1024        # 逐字稿文件 30MB（與 upload 一致）
_AUDIO_MAX_BYTES = 100 * 1024 * 1024  # 音檔放寬到 100MB
_MEMO_SOURCES_DIR = data_store.DATA_DIR / "uploads"

# 單一來源：欄位定義只在 memo_extractor.FIELDS 維護，MemoSave 由它 + interview_date 動態生成
MemoSave = create_model(
    "MemoSave",
    interview_date=(str, ""),
    **{key: (str, "") for key in memo_extractor.FIELD_KEYS},
)


async def _extract_text_content(filename: str, content: bytes) -> str:
    if Path(filename).suffix.lower() in {".txt", ".md"}:
        return content.decode("utf-8", errors="replace")
    try:
        return await asyncio.to_thread(extract_text, filename, content)
    except FileParseError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _save_memo_source(company_id: str, filename: str, content: bytes) -> dict:
    if not company_id or Path(company_id).name != company_id:
        raise HTTPException(status_code=400, detail="Invalid company_id")
    suffix = Path(filename).suffix.lower()
    stored_name = f"memo_source_{uuid.uuid4().hex}{suffix}"
    company_dir = _MEMO_SOURCES_DIR / company_id
    company_dir.mkdir(parents=True, exist_ok=True)
    (company_dir / stored_name).write_bytes(content)
    source = {
        "filename": Path(filename).name,
        "stored_name": stored_name,
        "url": f"/uploads/{company_id}/{stored_name}",
        "size": len(content),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    data_store.update_company(company_id, {"call_memo_source": source})
    return source


def _memo_source_path(company_id: str, source: dict) -> Path:
    stored_name = Path(source.get("stored_name") or "").name
    if not stored_name:
        raise HTTPException(status_code=404, detail="尚未保存逐字稿來源")
    path = _MEMO_SOURCES_DIR / company_id / stored_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="逐字稿原檔不存在，請重新上傳")
    return path


@router.get("/{company_id}/memo")
def get_memo(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company.get("call_memo") or {}


@router.get("/{company_id}/memo/source")
def get_memo_source(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company.get("call_memo_source") or {}


@router.put("/{company_id}/memo")
def save_memo(company_id: str, memo: MemoSave):
    company = data_store.update_company(company_id, {"call_memo": memo.model_dump()})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company.get("call_memo")


@router.post("/{company_id}/memo/extract")
async def extract_memo(company_id: str, file: UploadFile = File(...), ai: dict = Depends(ai_from_headers)):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    content = await file.read()
    if len(content) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="逐字稿檔過大，上限 30MB")
    filename = file.filename or "transcript.txt"

    transcript = await _extract_text_content(filename, content)

    if not transcript.strip():
        raise HTTPException(status_code=422, detail="無法從檔案中取得文字內容")

    _save_memo_source(company_id, filename, content)

    try:
        fields = await memo_extractor.extract_from_transcript(
            company["name"], transcript, source_filename=filename, **ai
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return fields


@router.post("/{company_id}/memo/reextract")
async def reextract_memo(company_id: str, ai: dict = Depends(ai_from_headers)):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    source = company.get("call_memo_source") or {}
    path = _memo_source_path(company_id, source)
    content = await asyncio.to_thread(path.read_bytes)
    filename = source.get("filename") or path.name
    transcript = await _extract_text_content(filename, content)
    if not transcript.strip():
        raise HTTPException(status_code=422, detail="無法從保存的逐字稿中取得文字內容")
    try:
        return await memo_extractor.extract_from_transcript(
            company["name"], transcript, source_filename=filename, **ai
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{company_id}/memo/transcribe-audio")
async def transcribe_audio_memo(
    company_id: str,
    file: UploadFile = File(...),
    ai: dict = Depends(ai_from_headers),
):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

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
        fields = await memo_extractor.extract_from_transcript(
            company["name"], transcript, source_filename=file.filename or "", **ai
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"transcript": transcript, "fields": fields}


@router.get("/{company_id}/memo/download")
def download_memo(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    memo = company.get("call_memo") or {}
    interview_date = memo.get("interview_date", date.today().strftime("%Y/%m/%d"))

    docx_bytes = memo_extractor.fill_template(company, memo, interview_date)

    safe_name = company["name"].replace("/", "-").replace("\\", "-")
    filename = f"Call Memo-{safe_name}_{interview_date.replace('/', '')}.docx"
    encoded_filename = quote(filename, safe="")

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        # Starlette headers must be Latin-1 encodable. RFC 5987 filename* keeps
        # Chinese company names without putting raw Unicode in the header.
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )
