"""Human review endpoints (Phase 4). Thin: all logic is in app/review/service.py.

The review queue itself is GET /invoices?status=needs_review (in invoices.py).
"""

import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Correction, Invoice
from app.db.session import get_db
from app.review import service
from app.schemas.invoice import (
    CorrectionOut,
    CorrectionRequest,
    InvoiceOut,
    RejectRequest,
    ResolveFlagRequest,
)

router = APIRouter(prefix="/invoices", tags=["review"])


def _run(db: Session, invoice_id: uuid.UUID, action: Callable[[], Any]) -> InvoiceOut:
    """Run a review action, map domain errors to HTTP, return the fresh invoice."""
    try:
        action()
    except service.NotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except service.InvalidState as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except service.InvalidCorrection as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, e.errors) from e
    invoice = db.scalar(
        select(Invoice)
        .options(selectinload(Invoice.flags))
        .where(Invoice.id == invoice_id)
        .execution_options(populate_existing=True)  # reload flags added during the action
    )
    return InvoiceOut.model_validate(invoice)


@router.patch("/{invoice_id}", response_model=InvoiceOut)
def correct_invoice(
    invoice_id: uuid.UUID, body: CorrectionRequest, db: Session = Depends(get_db)
) -> InvoiceOut:
    """Correct one or more fields. All-or-nothing; checks re-run afterwards."""
    changes = [service.FieldChange(c.field_path, c.value) for c in body.changes]
    return _run(
        db,
        invoice_id,
        lambda: service.apply_corrections(
            db, invoice_id, changes, body.corrected_by, body.note, today=date.today()
        ),
    )


@router.post("/{invoice_id}/flags/{flag_id}/resolve", response_model=InvoiceOut)
def resolve_flag(
    invoice_id: uuid.UUID,
    flag_id: uuid.UUID,
    body: ResolveFlagRequest,
    db: Session = Depends(get_db),
) -> InvoiceOut:
    """Accept a flagged value as correct (e.g. a real credit note, not a duplicate)."""
    return _run(
        db,
        invoice_id,
        lambda: service.resolve_flag(db, invoice_id, flag_id, body.resolved_by, body.note),
    )


@router.post("/{invoice_id}/reject", response_model=InvoiceOut)
def reject_invoice(
    invoice_id: uuid.UUID, body: RejectRequest, db: Session = Depends(get_db)
) -> InvoiceOut:
    return _run(
        db, invoice_id, lambda: service.reject_invoice(db, invoice_id, body.rejected_by, body.reason)
    )


@router.get("/{invoice_id}/corrections", response_model=list[CorrectionOut])
def list_corrections(invoice_id: uuid.UUID, db: Session = Depends(get_db)) -> list[CorrectionOut]:
    """The audit trail: every human change, oldest first."""
    if db.get(Invoice, invoice_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    rows = db.scalars(
        select(Correction)
        .where(Correction.invoice_id == invoice_id)
        .order_by(Correction.seq)
    )
    return [CorrectionOut.model_validate(r) for r in rows]
