import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on Postgres (indexable, queryable); plain JSON elsewhere.
JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class InvoiceStatus(str, enum.Enum):
    pending = "pending"  # waiting for a worker to pick it up
    processing = "processing"  # a worker has claimed it and is calling the LLM
    approved = "approved"
    needs_review = "needs_review"
    extraction_failed = "extraction_failed"


class Severity(str, enum.Enum):
    warning = "warning"
    error = "error"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # SHA-256 of the file bytes: the idempotency key. Same file -> same record.
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    file_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"),
        default=InvoiceStatus.pending,
        index=True,
    )
    extracted_json: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    # How many times a worker has picked this up. Stops a "poison" file that crashes
    # the server from being retried forever on every restart.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Normalized "vendor|invoice_number" for business-duplicate search. Indexed column,
    # not a JSON lookup, so it stays fast with millions of invoices. NOT unique: a
    # duplicate is flagged for a human, never silently rejected.
    dup_key: Mapped[str | None] = mapped_column(String(300), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    runs: Mapped[list["ExtractionRun"]] = relationship(back_populates="invoice")
    flags: Mapped[list["FieldFlag"]] = relationship(back_populates="invoice")
    corrections: Mapped[list["Correction"]] = relationship(back_populates="invoice")


class ExtractionRun(Base):
    """One row per LLM call. Raw output is stored BEFORE parsing (never lose evidence)."""

    __tablename__ = "extraction_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(20))
    raw_response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    invoice: Mapped[Invoice] = relationship(back_populates="runs")


class FieldFlag(Base):
    """A deterministic check (or low confidence) said this field needs a human."""

    __tablename__ = "field_flags"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    field_path: Mapped[str] = mapped_column(String(200))  # e.g. "line_items[2].unit_price"
    reason: Mapped[str] = mapped_column(Text)
    severity: Mapped[Severity] = mapped_column(Enum(Severity, name="flag_severity"))
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    invoice: Mapped[Invoice] = relationship(back_populates="flags")


class Correction(Base):
    """Append-only audit trail of human edits. Never overwrite, always append."""

    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    field_path: Mapped[str] = mapped_column(String(200))
    old_value: Mapped[Any] = mapped_column(JsonType, nullable=True)
    new_value: Mapped[Any] = mapped_column(JsonType, nullable=True)
    corrected_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    invoice: Mapped[Invoice] = relationship(back_populates="corrections")
