import hashlib
import uuid
from collections.abc import Callable

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db.models import Invoice, InvoiceStatus
from app.db.session import get_db
from app.extraction.client import Extractor
from app.extraction.deps import get_extractor, get_session_factory
from app.extraction.service import process_invoice
from app.schemas.invoice import InvoiceOut, UploadResult

router = APIRouter(prefix="/invoices", tags=["invoices"])

# Content type -> file extension. Anything else is rejected.
ALLOWED_TYPES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}

# Magic bytes: don't trust the client's Content-Type header alone.
MAGIC = {
    "application/pdf": (b"%PDF",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
}


def _get_by_hash(db: Session, file_hash: str) -> Invoice | None:
    return db.scalar(
        select(Invoice).options(selectinload(Invoice.flags)).where(Invoice.file_hash == file_hash)
    )


@router.post("", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
async def upload_invoice(
    file: UploadFile,
    response: Response,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    extractor: Extractor = Depends(get_extractor),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> UploadResult:
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported type {file.content_type!r}. Allowed: {', '.join(ALLOWED_TYPES)}",
        )

    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"File exceeds {settings.max_upload_mb} MB"
        )
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file")
    if not data.startswith(MAGIC[file.content_type]):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"File contents don't match {file.content_type}"
        )

    # Idempotency: same bytes -> same invoice, no second LLM call later.
    file_hash = hashlib.sha256(data).hexdigest()
    existing = _get_by_hash(db, file_hash)
    if existing:
        response.status_code = status.HTTP_200_OK
        return UploadResult(invoice=InvoiceOut.model_validate(existing), duplicate=True)

    # Store by hash, not by user filename (avoids path tricks and collisions).
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path = settings.upload_dir / f"{file_hash}{ALLOWED_TYPES[file.content_type]}"
    path.write_bytes(data)

    invoice = Invoice(
        file_hash=file_hash,
        original_filename=(file.filename or "upload")[:255],
        content_type=file.content_type,
        file_path=str(path),
        status=InvoiceStatus.pending,
    )
    db.add(invoice)
    try:
        db.commit()
    except IntegrityError:
        # Two identical uploads raced; the other one won. Return its record.
        db.rollback()
        existing = _get_by_hash(db, file_hash)
        response.status_code = status.HTTP_200_OK
        return UploadResult(invoice=InvoiceOut.model_validate(existing), duplicate=True)

    db.refresh(invoice, attribute_names=["flags", "created_at", "updated_at"])
    # Respond immediately (status=pending); extraction runs after the response is sent.
    background.add_task(process_invoice, invoice.id, session_factory, extractor, settings)
    return UploadResult(invoice=InvoiceOut.model_validate(invoice), duplicate=False)


@router.post("/{invoice_id}/retry", response_model=InvoiceOut, status_code=status.HTTP_202_ACCEPTED)
def retry_extraction(
    invoice_id: uuid.UUID,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    extractor: Extractor = Depends(get_extractor),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> InvoiceOut:
    """Re-run extraction for an invoice that failed (e.g. Gemini was overloaded)."""
    invoice = db.scalar(
        select(Invoice).options(selectinload(Invoice.flags)).where(Invoice.id == invoice_id)
    )
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    if invoice.status != InvoiceStatus.extraction_failed:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Only failed extractions can be retried (status={invoice.status.value})"
        )
    invoice.status = InvoiceStatus.pending
    invoice.attempts = 0  # a human asked for it: give it a fresh set of attempts
    db.commit()
    background.add_task(process_invoice, invoice.id, session_factory, extractor, settings)
    return InvoiceOut.model_validate(invoice)


@router.get("/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: uuid.UUID, db: Session = Depends(get_db)) -> InvoiceOut:
    invoice = db.scalar(
        select(Invoice).options(selectinload(Invoice.flags)).where(Invoice.id == invoice_id)
    )
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return InvoiceOut.model_validate(invoice)


@router.get("", response_model=list[InvoiceOut])
def list_invoices(
    status_filter: InvoiceStatus | None = Query(None, alias="status"),
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[InvoiceOut]:
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.flags))
        .order_by(Invoice.created_at.desc())
        .limit(min(limit, 200))
        .offset(offset)
    )
    if status_filter is not None:
        stmt = stmt.where(Invoice.status == status_filter)
    return [InvoiceOut.model_validate(i) for i in db.scalars(stmt)]
