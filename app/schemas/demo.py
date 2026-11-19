"""Response shapes of the public demo's routes (app/api/demo.py)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.invoice import InvoiceOut


class RunOut(BaseModel):
    """One LLM call (an extraction_runs row), without the raw text."""

    model_config = ConfigDict(from_attributes=True)

    model: str  # `replay:<model>` for a recorded answer
    prompt_version: str
    error: str | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None  # None for replays (no Gemini call) and unknown models
    created_at: datetime


class DemoInvoiceOut(InvoiceOut):
    checks_as_of: date | None  # the "today" the date checks used (a replay pins its recording's day)
    runs: list[RunOut]



class GenerateIn(BaseModel):
    plant: str | None = None  # a key of GET /demo/status `plants`; None = a clean invoice


class GenerateOut(BaseModel):
    invoice: DemoInvoiceOut  # pending: poll GET /demo/invoices/{id}
    description: str
    printed: dict[str, Any]  # what the generated PDF really says (the planted mistake included)
    planted: list[str]  # the check codes the planted mistake must trigger


class DemoStatus(BaseModel):
    live_model: str
    live_daily_limit: int
    live_used_today: int
    live_left_today: int
    live_per_ip_per_hour: int
    max_upload_mb: int
    plants: dict[str, str]  # what "generate" can plant: key -> plain-English label
