"""FastAPI dependencies for the extraction job. Tests override these with fakes."""

from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import SessionLocal
from app.extraction.client import Extractor, GeminiExtractor


@lru_cache
def _gemini(api_key: str, timeout_s: int, thinking_level: str) -> GeminiExtractor:
    return GeminiExtractor(api_key, timeout_s=timeout_s, thinking_level=thinking_level)


def get_extractor(settings: Settings = Depends(get_settings)) -> Extractor:
    return _gemini(settings.gemini_api_key, settings.gemini_timeout_s, settings.gemini_thinking_level)


def get_session_factory() -> Callable[[], Session]:
    # Background jobs outlive the request, so they open their own DB session.
    return SessionLocal
