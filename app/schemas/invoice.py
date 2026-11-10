"""API request/response shapes. (The LLM extraction schema lives in extraction.py.)"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import InvoiceStatus, Severity


class FlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    field_path: str
    reason: str
    severity: Severity
    resolved: bool
    resolved_by: str | None
    resolved_at: datetime | None
    resolution_note: str | None
    created_at: datetime


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    content_type: str
    file_hash: str
    status: InvoiceStatus
    extracted_json: dict[str, Any] | None  # what the LLM said (never changes)
    corrected_json: dict[str, Any] | None  # after human corrections (null if none)
    flags: list[FlagOut] = []
    created_at: datetime
    updated_at: datetime


class UploadResult(BaseModel):
    invoice: InvoiceOut
    duplicate: bool  # True = this exact file was uploaded before; existing record returned


# --- review (Phase 4) ---------------------------------------------------------

# A reviewer name: stripped, non-blank.
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class FieldChangeIn(BaseModel):
    field_path: str = Field(examples=["line_items[0].amount"])
    # Send money as a string ("20.00"): JSON numbers become floats on the way in.
    value: Any = Field(examples=["20.00"])


class CorrectionRequest(BaseModel):
    corrected_by: Name  # no auth yet (Phase 6+): reviewer names themself
    note: str | None = None
    changes: list[FieldChangeIn] = Field(min_length=1)


class ResolveFlagRequest(BaseModel):
    resolved_by: Name
    note: str | None = None


class RejectRequest(BaseModel):
    rejected_by: Name
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CorrectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    field_path: str
    old_value: Any
    new_value: Any
    corrected_by: str
    note: str | None
    created_at: datetime
