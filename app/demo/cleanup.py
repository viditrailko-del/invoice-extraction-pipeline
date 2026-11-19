"""Delete demo invoices with everything that hangs off them (runs, flags, corrections).

Only for the public demo: a replay replacing a visitor's earlier copy, and the nightly reset. Outside the demo
nothing is ever deleted (CLAUDE.md: never overwrite history).
"""

import uuid

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models import Correction, ExtractionRun, FieldFlag, Invoice


def delete_invoices(db: Session, ids: list[uuid.UUID]) -> int:
    if not ids:
        return 0
    for child in (Correction, FieldFlag, ExtractionRun):
        db.execute(delete(child).where(child.invoice_id.in_(ids)))
    db.execute(delete(Invoice).where(Invoice.id.in_(ids)))
    db.commit()
    return len(ids)
