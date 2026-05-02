"""FastAPI dependencies for extracting AI provider config from request."""
import os
from fastapi import Header, Query


def ai_from_headers(
    x_api_key: str = Header(default=""),
    x_ai_provider: str = Header(default=""),
) -> dict:
    return {
        "api_key": x_api_key or os.getenv("ANTHROPIC_API_KEY", ""),
        "provider": (x_ai_provider or "anthropic").lower(),
    }


def ai_from_query(
    api_key: str = Query(default=""),
    provider: str = Query(default=""),
) -> dict:
    return {
        "api_key": api_key or os.getenv("ANTHROPIC_API_KEY", ""),
        "provider": (provider or "anthropic").lower(),
    }
