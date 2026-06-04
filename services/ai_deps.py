"""FastAPI dependencies for extracting the AI engine choice from a request.

Local-only: the engine selects which local CLI / endpoint to use
(claude | codex | gemini | ollama). No API keys.
"""
import os
from fastapi import Header, Query


def _resolve(engine: str) -> dict:
    engine = (engine or os.getenv("AI_ENGINE", "claude") or "claude").strip().lower()
    return {"engine": engine}


def ai_from_headers(x_ai_engine: str = Header(default="")) -> dict:
    return _resolve(x_ai_engine)


def ai_from_query(engine: str = Query(default="")) -> dict:
    return _resolve(engine)
