import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile

from services import company_extractor, file_parser
from services.ai_deps import ai_from_headers

router = APIRouter(prefix="/api", tags=["upload"])
log = logging.getLogger("upload")

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}


@router.post("/upload")
async def upload_file(file: UploadFile = File(...), ai: dict = Depends(ai_from_headers)):
    content = await file.read()
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()
    suggested_label = Path(filename).stem
    log.info("Upload: %s (%d bytes)", filename, len(content))

    if ext in _IMAGE_EXTS:
        log.info("Image upload detected, using Claude visual recognition")
        groups = await company_extractor.extract_companies_from_image(content, ext, suggested_label, **ai)
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

    groups = company_extractor.extract_companies_from_text(text, suggested_label, **ai)
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
