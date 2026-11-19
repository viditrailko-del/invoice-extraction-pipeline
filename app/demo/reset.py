"""Empty the public demo: delete every visitor's invoices and the files they uploaded or generated.

Used by the nightly POST /demo/reset (GitHub Actions) and by `python -m demo.reset` locally. Only sandboxed rows
(`demo_session IS NOT NULL`) are deleted, with their runs, flags and corrections; an invoice outside any sandbox
is never touched. A file is deleted only when it lives under UPLOAD_DIR: the replay scenarios' documents ship
with the app (app/demo/scenarios/files) and stay.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Invoice
from app.demo.cleanup import delete_invoices


def reset_demo(db: Session, settings: Settings) -> dict[str, int]:
    rows = db.execute(select(Invoice.id, Invoice.file_path).where(Invoice.demo_session.is_not(None))).all()
    ids: list[uuid.UUID] = [r.id for r in rows]
    upload_dir = settings.upload_dir.resolve()
    files = 0
    for path in {Path(r.file_path).resolve() for r in rows}:
        if path.is_relative_to(upload_dir) and path.is_file():
            path.unlink(missing_ok=True)
            files += 1
    delete_invoices(db, ids)
    return {"invoices_deleted": len(ids), "files_deleted": files}
