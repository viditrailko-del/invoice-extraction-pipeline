from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
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
    prompt_version: str = "v3"  # v3 adds tax_breakdown
    llm_retry_backoff_s: float = 2.0  # waits 2s, then 4s, between retries
    recover_on_startup: bool = True  # re-run invoices stuck by a crash/restart
    log_format: str = "text"  # "json" in the production image: one JSON object per line
    log_level: str = "INFO"

    # --- public demo (Phase 7; app/demo) ---
    demo_mode: bool = False  # /demo/* routes and the demo page; every invoice lives in a visitor sandbox
    demo_live_model: str = "gemini-3.5-flash-lite"  # live extractions only; no fallback model
    demo_live_daily_limit: int = 20  # live extractions per UTC day for the whole demo (free tier)
    demo_live_per_ip_per_hour: int = 3
    demo_reset_token: str = ""  # POST /demo/reset (nightly); empty = route off

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        # Hosted Postgres (Neon, Render) hands out postgres:// or postgresql:// URLs; SQLAlchemy would pick
        # psycopg2, which isn't installed. Same database, our driver.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
