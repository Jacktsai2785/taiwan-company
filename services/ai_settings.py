"""Single source of truth for the platform-wide local AI engine."""
import os

from services import data_store

KNOWN_ENGINES = ("claude", "codex", "gemini", "ollama")


def get_engine() -> str:
    configured = str(data_store.get_config().get("ai_engine") or "").strip().lower()
    fallback = (os.getenv("AI_ENGINE", "claude") or "claude").strip().lower()
    engine = configured or fallback
    return engine if engine in KNOWN_ENGINES else "claude"


def is_configured() -> bool:
    return bool(str(data_store.get_config().get("ai_engine") or "").strip())


def set_engine(engine: str) -> str:
    engine = (engine or "").strip().lower()
    if engine not in KNOWN_ENGINES:
        raise ValueError(f"不支援的 AI 引擎：{engine or '（空白）'}")
    data_store.save_ai_engine(engine)
    return engine
