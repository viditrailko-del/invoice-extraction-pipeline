from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All config comes from environment variables / .env. No hard-coded secrets."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://invoice:invoice@localhost:5432/invoices"
    upload_dir: Path = Path("storage")
    max_upload_mb: int = 20
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_fallback_model: str = "gemini-3.5-flash-lite"  # used when the main one is down
    gemini_thinking_level: str = "low"  # "high" was 25-50x slower with no accuracy gain
    gemini_timeout_s: int = 60
    prompt_version: str = "v1"
    llm_retry_backoff_s: float = 2.0  # waits 2s, then 4s, between retries
    recover_on_startup: bool = True  # re-run invoices stuck by a crash/restart


@lru_cache
def get_settings() -> Settings:
    return Settings()
