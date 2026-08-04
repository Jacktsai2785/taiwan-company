"""FastAPI dependencies for extracting the AI engine choice from a request.

Local-only: the engine selects which local CLI / endpoint to use
(claude | codex | gemini | ollama). No API keys.
"""
from fastapi import Header, Query

from services import ai_settings


def _resolve(engine: str) -> dict:
    # The server-side setting is authoritative. Request values remain accepted
    # for backwards compatibility but cannot silently override the global choice.
    return {"engine": ai_settings.get_engine()}


def ai_from_headers(x_ai_engine: str = Header(default="")) -> dict:
    return _resolve(x_ai_engine)


def ai_from_query(engine: str = Query(default="")) -> dict:
    return _resolve(engine)
