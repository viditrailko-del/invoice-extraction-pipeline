"""API response shapes. (The LLM extraction schema — Invoice/LineItem/Vendor — arrives in Phase 2.)"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.models import InvoiceStatus, Severity


class FlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    field_path: str
    reason: str
    severity: Severity
    resolved: bool


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    content_type: str
    file_hash: str
    status: InvoiceStatus
    extracted_json: dict[str, Any] | None
    flags: list[FlagOut] = []
    created_at: datetime
    updated_at: datetime


class UploadResult(BaseModel):
    invoice: InvoiceOut
    duplicate: bool  # True = this exact file was uploaded before; existing record returned
