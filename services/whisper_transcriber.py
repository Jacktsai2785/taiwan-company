import asyncio
import os
import shutil
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

    # whisper 不論輸入格式一律透過 subprocess 呼叫系統的 ffmpeg 解碼——套件裝了但
    # ffmpeg 不在 PATH 時，原本會在 model.transcribe() 內部深處噴 FileNotFoundError，
    # 對使用者只看到裸的 500。這裡提前擋下，給可行動的訊息。
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "音訊轉文字功能需要系統安裝 ffmpeg，但目前找不到。"
            "請執行 sudo apt install ffmpeg 安裝後再試一次。"
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
