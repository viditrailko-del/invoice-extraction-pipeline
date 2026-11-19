"""Empty the public demo: delete visitors' invoices from before today (UTC) and the files they uploaded or generated.

Used by the nightly POST /demo/reset (GitHub Actions, 00:05 UTC) and by `python -m demo.reset` locally. Only
sandboxed rows (`demo_session IS NOT NULL`) are deleted, with their runs, flags and corrections; an invoice outside
any sandbox is never touched.

Today's invoices stay (created today, or extracted today): the live quota is re-counted from today's extraction
runs after a restart, so deleting them would hand out a fresh day. At 00:05 that keeps only the first minutes of
the day, which go the next night.

A file is deleted only when it lives under UPLOAD_DIR and no remaining invoice points to it (files are named by
their hash, so two sandboxes can share one). The replay scenarios' documents ship with the app and stay.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time
from pathlib import Path

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ExtractionRun, Invoice
from app.demo.cleanup import delete_invoices


def reset_demo(db: Session, settings: Settings, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    start_of_day = datetime.combine(now.astimezone(UTC).date(), time(), UTC)
    ran_today = exists().where(ExtractionRun.invoice_id == Invoice.id, ExtractionRun.created_at >= start_of_day)
    rows = db.execute(
        select(Invoice.id, Invoice.file_path)
        .where(Invoice.demo_session.is_not(None), Invoice.created_at < start_of_day, ~ran_today)
    ).all()
    ids: list[uuid.UUID] = [r.id for r in rows]
    delete_invoices(db, ids)

    still_used = {Path(p).resolve() for p in db.scalars(select(Invoice.file_path))}
    upload_dir = settings.upload_dir.resolve()
    files = 0
    for path in {Path(r.file_path).resolve() for r in rows} - still_used:
        if path.is_relative_to(upload_dir) and path.is_file():
            path.unlink(missing_ok=True)
            files += 1
    return {"invoices_deleted": len(ids), "files_deleted": files}
