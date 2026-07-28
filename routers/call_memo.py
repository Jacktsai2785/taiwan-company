import asyncio
from datetime import date
from pathlib import Path

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

# 單一來源：欄位定義只在 memo_extractor.FIELDS 維護，MemoSave 由它 + interview_date 動態生成
MemoSave = create_model(
    "MemoSave",
    interview_date=(str, ""),
    **{key: (str, "") for key in memo_extractor.FIELD_KEYS},
)


@router.get("/{company_id}/memo")
def get_memo(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company.get("call_memo") or {}


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

    if filename.lower().endswith(".txt"):
        transcript = content.decode("utf-8", errors="replace")
    else:
        # 含 OCR/解析的同步呼叫卸載到 thread，否則卡死整個 event loop（比照 upload.py）
        try:
            transcript = await asyncio.to_thread(extract_text, filename, content)
        except FileParseError as e:
            raise HTTPException(status_code=422, detail=str(e))

    if not transcript.strip():
        raise HTTPException(status_code=422, detail="無法從檔案中取得文字內容")

    try:
        fields = await memo_extractor.extract_from_transcript(company["name"], transcript, **ai)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # 訪談日期優先用逐字稿抽到的；抽不到才預設今天（不再硬蓋）
    if not fields.get("interview_date"):
        fields["interview_date"] = date.today().strftime("%Y/%m/%d")
    return fields


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
        fields = await memo_extractor.extract_from_transcript(company["name"], transcript, **ai)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not fields.get("interview_date"):
        fields["interview_date"] = date.today().strftime("%Y/%m/%d")
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

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
