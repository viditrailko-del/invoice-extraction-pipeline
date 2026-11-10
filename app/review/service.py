"""Human review: corrections, resolving flags, rejecting.

Rules this module enforces:
  * extracted_json (the LLM's answer) is never modified; edits go to corrected_json.
  * Every changed field appends a `corrections` row (old -> new, who, why).
  * Flags are never deleted. When a correction fixes a problem, its flag is marked
    resolved by "system:recheck"; a human can resolve one with a note.
  * Status is derived: no unresolved flags -> approved, otherwise needs_review.

Routes translate the exceptions below into HTTP codes (404 / 409 / 422).
"""

import copy
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.checks.duplicates import DUPLICATE_CODE, make_dup_key
from app.checks.runner import run_checks
from app.db.models import Correction, FieldFlag, Invoice, InvoiceStatus
from app.extraction.service import flag_business_duplicates, store_check_flags
from app.review.paths import InvalidPath, get_at, set_at
from app.schemas.extraction import ExtractedInvoice

SYSTEM_RECHECK = "system:recheck"
REVIEWABLE = (InvoiceStatus.needs_review, InvoiceStatus.approved)
REJECTABLE = (*REVIEWABLE, InvoiceStatus.extraction_failed)


class NotFound(Exception):
    pass


class InvalidState(Exception):
    pass


class InvalidCorrection(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class FieldChange:
    field_path: str
    value: Any


def _lock_invoice(db: Session, invoice_id: uuid.UUID) -> Invoice:
    """SELECT ... FOR UPDATE: two reviewers editing the same invoice at once are
    serialized, so neither silently loses the other's correction."""
    invoice = db.scalar(select(Invoice).where(Invoice.id == invoice_id).with_for_update())
    if invoice is None:
        raise NotFound("Invoice not found")
    return invoice


def apply_corrections(
    db: Session,
    invoice_id: uuid.UUID,
    changes: list[FieldChange],
    corrected_by: str,
    note: str | None,
    today: date,
) -> Invoice:
    invoice = _lock_invoice(db, invoice_id)
    if invoice.status not in REVIEWABLE:
        raise InvalidState(f"Only reviewable invoices can be corrected (status={invoice.status.value})")

    before = invoice.current_json
    assert before is not None  # needs_review/approved always have extracted data
    draft = copy.deepcopy(before)
    try:
        for ch in changes:
            set_at(draft, ch.field_path, ch.value)
    except InvalidPath as e:
        raise InvalidCorrection([str(e)]) from e

    # The corrected invoice must still satisfy the same schema the LLM had to.
    try:
        corrected = ExtractedInvoice.model_validate(draft)
    except ValidationError as e:
        raise InvalidCorrection(
            [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        ) from e
    after = corrected.model_dump(mode="json")

    # Compare as Python values (Decimal, date), not JSON text: "22" and "22.00" are
    # the same money and must not create a fake correction. Strings stay exact.
    before_py = ExtractedInvoice.model_validate(before).model_dump()
    after_py = corrected.model_dump()

    changed = False
    for path in dict.fromkeys(ch.field_path for ch in changes):  # dedupe, keep order
        if _get_or_none(before_py, path) != _get_or_none(after_py, path):
            old, new = _get_or_none(before, path), _get_or_none(after, path)
            changed = True
            db.add(
                Correction(
                    invoice_id=invoice.id,
                    field_path=path,
                    old_value=old,
                    new_value=new,
                    corrected_by=corrected_by,
                    note=note,
                )
            )
    if not changed:
        return invoice  # nothing actually differed: no history, no re-check

    invoice.corrected_json = after
    _recheck(db, invoice, corrected, today)
    _derive_status(db, invoice)
    db.commit()
    return invoice


def resolve_flag(
    db: Session, invoice_id: uuid.UUID, flag_id: uuid.UUID, resolved_by: str, note: str | None
) -> Invoice:
    """A human says "this flag is fine as is" (real credit note, not a duplicate...)."""
    invoice = _lock_invoice(db, invoice_id)
    if invoice.status not in REVIEWABLE:
        raise InvalidState(f"Flags can't be resolved in status {invoice.status.value}")
    flag = db.get(FieldFlag, flag_id)
    if flag is None or flag.invoice_id != invoice.id:
        raise NotFound("Flag not found on this invoice")
    if flag.resolved:
        raise InvalidState(f"Flag already resolved by {flag.resolved_by}")
    _resolve(flag, resolved_by, note)
    _derive_status(db, invoice)
    db.commit()
    return invoice


def reject_invoice(db: Session, invoice_id: uuid.UUID, rejected_by: str, reason: str) -> Invoice:
    """Terminal decision (confirmed duplicate, junk). Recorded in corrections as a
    status change, so the audit trail shows who rejected it and why."""
    invoice = _lock_invoice(db, invoice_id)
    if invoice.status not in REJECTABLE:
        raise InvalidState(f"Can't reject an invoice in status {invoice.status.value}")
    db.add(
        Correction(
            invoice_id=invoice.id,
            field_path="status",
            old_value=invoice.status.value,
            new_value=InvoiceStatus.rejected.value,
            corrected_by=rejected_by,
            note=reason,
        )
    )
    invoice.status = InvoiceStatus.rejected
    db.commit()
    return invoice


# --- internals ----------------------------------------------------------------


def _get_or_none(data: Any, path: str) -> Any:
    try:
        return get_at(data, path)
    except InvalidPath:
        return None  # e.g. a line that only exists after "line_items" was replaced


def _flag_key(code: str, field_path: str, reason: str) -> tuple[str, str, str]:
    return code, field_path, reason


def _resolve(flag: FieldFlag, by: str, note: str | None) -> None:
    flag.resolved = True
    flag.resolved_by = by
    flag.resolved_at = datetime.now(timezone.utc)
    flag.resolution_note = note


def _recheck(db: Session, invoice: Invoice, corrected: ExtractedInvoice, today: date) -> None:
    """Re-run every check on the corrected data and reconcile with existing flags.

    A flag is identified by (code, field_path, reason). The reason contains the
    numbers, so "20 x 10 != 25" and "20 x 10 != 30" are different problems.
      * open flag, problem gone        -> resolved by system:recheck
      * open flag, problem still there -> left as is
      * new problem                    -> new flag
      * problem a HUMAN already accepted -> not raised again
    """
    flags = invoice.flags
    results = run_checks(corrected, today)
    result_keys = {_flag_key(r.code, r.field_path, r.reason) for r in results}

    for f in flags:
        if not f.resolved and f.code != DUPLICATE_CODE:
            if _flag_key(f.code, f.field_path, f.reason) not in result_keys:
                _resolve(f, SYSTEM_RECHECK, "No longer fails after correction")

    known = {
        _flag_key(f.code, f.field_path, f.reason)
        for f in flags
        if not f.resolved or f.resolved_by != SYSTEM_RECHECK
    }
    store_check_flags(
        db, invoice, [r for r in results if _flag_key(r.code, r.field_path, r.reason) not in known]
    )

    # Vendor or invoice number corrected -> it's a different identity: old duplicate
    # matches no longer apply, look for new ones.
    if make_dup_key(corrected) != invoice.dup_key:
        for f in flags:
            if not f.resolved and f.code == DUPLICATE_CODE:
                _resolve(f, SYSTEM_RECHECK, "Vendor/invoice number corrected")
        flag_business_duplicates(db, invoice, corrected)


def _derive_status(db: Session, invoice: Invoice) -> None:
    db.flush()  # make new flag rows visible to the count below
    open_flags = db.scalar(
        select(func.count())
        .select_from(FieldFlag)
        .where(FieldFlag.invoice_id == invoice.id, FieldFlag.resolved.is_(False))
    )
    invoice.status = InvoiceStatus.needs_review if open_flags else InvoiceStatus.approved
