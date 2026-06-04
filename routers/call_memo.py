from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

from services import data_store, memo_extractor
from services.ai_deps import ai_from_headers
from services.file_parser import extract_text
from services import whisper_transcriber

router = APIRouter(prefix="/api/companies", tags=["call_memo"])


class MemoSave(BaseModel):
    interview_date: str = ""
    deal_source: str = ""
    interviewees: str = ""
    paid_in_capital: str = ""
    address: str = ""
    founding_date: str = ""
    underwriter: str = ""
    auditor: str = ""
    chairman: str = ""
    general_manager: str = ""
    headcount: str = ""
    ipo_timeline: str = ""
    investment_terms: str = ""
    business_revenue: str = ""
    financials: str = ""
    management_team: str = ""
    board_shareholding: str = ""
    recent_development: str = ""
    major_customers: str = ""
    major_suppliers: str = ""
    factory_capacity: str = ""
    competitors: str = ""
    industry_trends: str = ""
    risk_tracking: str = ""
    conclusion: str = ""


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
    filename = file.filename or "transcript.txt"

    if filename.lower().endswith(".txt"):
        transcript = content.decode("utf-8", errors="replace")
    else:
        transcript = extract_text(filename, content)

    if not transcript.strip():
        raise HTTPException(status_code=422, detail="無法從檔案中取得文字內容")

    fields = await memo_extractor.extract_from_transcript(company["name"], transcript, **ai)
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
    transcript = await whisper_transcriber.transcribe_audio(content, suffix)

    if not transcript.strip():
        raise HTTPException(status_code=422, detail="無法辨識音訊內容，請確認檔案包含清晰語音")

    fields = await memo_extractor.extract_from_transcript(company["name"], transcript, **ai)
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
