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


@lru_cache
def get_settings() -> Settings:
    return Settings()
