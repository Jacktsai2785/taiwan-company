import asyncio
import os
import tempfile

SUPPORTED_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac", ".aac", ".wma", ".mp4"}

# Cache loaded model to avoid reloading on every request
_model_cache: dict = {}


def _transcribe_sync(file_bytes: bytes, suffix: str, model_name: str) -> str:
    try:
        import whisper
    except ImportError:
        raise RuntimeError(
            "音訊轉文字功能需要 openai-whisper 套件，但目前未安裝。"
            "請執行 make setup 重裝依賴，或手動 pip install openai-whisper（並確認系統有 ffmpeg）。"
        )

    if model_name not in _model_cache:
        _model_cache[model_name] = whisper.load_model(model_name)
    model = _model_cache[model_name]

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(file_bytes)
        tmp_path = f.name

    try:
        result = model.transcribe(tmp_path, language="zh", fp16=False)
        return result["text"].strip()
    finally:
        os.unlink(tmp_path)


async def transcribe_audio(file_bytes: bytes, suffix: str, model_name: str = "small") -> str:
    """Transcribe audio file bytes using Whisper. Returns Mandarin transcript text."""
    return await asyncio.to_thread(_transcribe_sync, file_bytes, suffix, model_name)
