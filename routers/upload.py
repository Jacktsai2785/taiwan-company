import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from services import company_extractor, file_parser
from services.ai_deps import ai_from_headers

router = APIRouter(prefix="/api", tags=["upload"])
log = logging.getLogger("upload")

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
# 文字/文件類由 file_parser 處理；與 materials.py 同步保持白名單一致。
_DOC_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".txt", ".csv"}
_ACCEPTED_EXTS = _IMAGE_EXTS | _DOC_EXTS
_MAX_BYTES = 30 * 1024 * 1024  # 30MB，避免單一大檔把單機 process 的記憶體吃爆
_AI_ERROR_HINT = (
    "AI 引擎未就緒或逾時，無法辨識（這不代表檔案沒有公司）。"
    "請確認已執行 `claude` 完成登入，或在側邊欄 ⚙ 換一個引擎後重試。"
)


@router.post("/upload")
async def upload_file(file: UploadFile = File(...), ai: dict = Depends(ai_from_headers)):
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()
    suggested_label = Path(filename).stem
    if ext not in _ACCEPTED_EXTS:
        raise HTTPException(
            status_code=415,
            detail=f"不支援的檔案格式「{ext or '（無副檔名）'}」，僅接受圖片與 PDF/Word/PPT/Excel/文字檔。",
        )
    content = await file.read()
    if len(content) > _MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"檔案過大（{len(content) // 1024 // 1024}MB），上限 30MB，請壓縮或分批上傳。",
        )
    log.info("Upload: %s (%d bytes)", filename, len(content))

    if ext in _IMAGE_EXTS:
        log.info("Image upload detected, using Claude visual recognition")
        try:
            groups = await company_extractor.extract_companies_from_image(content, ext, suggested_label, **ai)
        except company_extractor.ExtractionError as e:
            raise HTTPException(status_code=503, detail=_AI_ERROR_HINT) from e
        log.info(
            "Found: valid=%d excluded=%d uncertain=%d",
            len(groups["valid"]), len(groups["excluded"]), len(groups["uncertain"])
        )
        return {
            "filename": filename,
            "suggested_label": suggested_label,
            "valid": groups["valid"],
            "excluded": groups["excluded"],
            "uncertain": groups["uncertain"],
            "extracted_chars": 0,
        }

    text = file_parser.extract_text(filename, content)
    log.info("Extracted %d chars", len(text))
    log.info("Text preview: %s", text[:300].replace("\n", " "))

    if text.startswith("[") and text.rstrip().endswith("]"):
        return {
            "filename": filename,
            "suggested_label": suggested_label,
            "valid": [],
            "excluded": [],
            "uncertain": [],
            "extracted_chars": 0,
            "ocr_failed": True,
            "ocr_message": text[1:-1],
        }

    try:
        groups = company_extractor.extract_companies_from_text(text, suggested_label, **ai)
    except company_extractor.ExtractionError as e:
        raise HTTPException(status_code=503, detail=_AI_ERROR_HINT) from e
    log.info(
        "Found: valid=%d excluded=%d uncertain=%d",
        len(groups["valid"]), len(groups["excluded"]), len(groups["uncertain"])
    )
    return {
        "filename": filename,
        "suggested_label": suggested_label,
        "valid": groups["valid"],
        "excluded": groups["excluded"],
        "uncertain": groups["uncertain"],
        "extracted_chars": len(text),
    }
