"""The reliability layer around the LLM call.

process_invoice() is the whole Phase 2 job for one invoice:

  1-3. extraction/core.py: call the LLM (retry, fallback model), validate with
     Pydantic (one retry with the error fed back). Every call is saved to
     extraction_runs by DbRecorder, raw text first, before parsing.
  4. Anything still failing -> status=extraction_failed. Never crash.
  5. Run deterministic checks + duplicate detection -> field_flags.
     Zero flags -> approved. ANY flag (warning or error) -> needs_review.
"""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.checks.base import Flag
from app.checks.duplicates import DUPLICATE_CODE, make_dup_key
from app.checks.runner import run_checks
from app.config import Settings
from app.db.models import ExtractionRun, FieldFlag, Invoice, InvoiceStatus, Severity
from app.extraction.client import Extractor
from app.extraction.core import Attempt, extract_invoice
from app.logging_setup import log_event
from app.schemas.extraction import ExtractedInvoice

log = logging.getLogger(__name__)

MAX_JOB_ATTEMPTS = 3  # a file that kills the worker 3 times is given up on


def process_invoice(
    invoice_id: uuid.UUID,
    session_factory: Callable[[], Session],
    extractor: Extractor,
    settings: Settings,
) -> None:
    with session_factory() as db:
        if not _claim(db, invoice_id):
            return  # someone else has it, or it's already done: never double-process
        invoice = db.get(Invoice, invoice_id)
        assert invoice is not None
        started = time.monotonic()
        recorder = DbRecorder(db, invoice)

        try:
            extracted = _extract(db, invoice, extractor, settings, recorder)
        except Exception:
            # Bug or unexpected error: record it, mark failed, keep the server alive.
            log_event(log, logging.ERROR, "extraction_crashed", exc_info=True, invoice_id=invoice_id)
            db.rollback()
            extracted = None

        flag_codes: list[str] = []
        if extracted is None:
            invoice.status = InvoiceStatus.extraction_failed
        else:
            invoice.extracted_json = extracted.model_dump(mode="json")  # Decimal -> exact string
            flags = run_checks(extracted, date.today())
            store_check_flags(db, invoice, flags)
            flag_codes = [f.code for f in flags]
            flag_codes += [DUPLICATE_CODE] * flag_business_duplicates(db, invoice, extracted)
            # Safe to be wrong: auto-approve only when nothing at all looked off.
            invoice.status = InvoiceStatus.needs_review if flag_codes else InvoiceStatus.approved
        invoice.processing_started_at = None
        db.commit()
        _log_finished(invoice, recorder.attempts, flag_codes, started, settings)


def _log_finished(
    invoice: Invoice, attempts: list[Attempt], flag_codes: list[str], started: float,
    settings: Settings,
) -> None:
    """One structured event per extraction: everything needed to watch cost, latency,
    retries and the review rate without querying the database."""
    answered = [a for a in attempts if a.raw_response is not None and not a.error]
    costs = [a.cost_usd for a in attempts if a.cost_usd is not None]
    log_event(
        log,
        logging.INFO,
        "extraction_finished",
        invoice_id=invoice.id,
        status=invoice.status.value,
        job_attempt=invoice.attempts,
        llm_calls=len(attempts),
        failed_calls=sum(1 for a in attempts if a.error),
        model=answered[-1].model if answered else None,
        prompt_version=settings.prompt_version,
        llm_latency_ms=sum(a.latency_ms or 0 for a in attempts),
        wall_ms=int((time.monotonic() - started) * 1000),
        input_tokens=sum(a.input_tokens or 0 for a in attempts),
        output_tokens=sum((a.output_tokens or 0) + (a.thinking_tokens or 0) for a in attempts),
        cost_usd=sum(costs) if costs else None,
        flags=sorted(flag_codes),
    )


def _claim(db: Session, invoice_id: uuid.UUID) -> bool:
    """Atomically move pending -> processing. Only ONE caller can win.

    A single UPDATE ... WHERE status='pending' is atomic in Postgres: if two workers
    race for the same invoice, exactly one sees a row come back.
    """
    claimed = db.execute(
        update(Invoice)
        .where(Invoice.id == invoice_id, Invoice.status == InvoiceStatus.pending)
        .values(
            status=InvoiceStatus.processing,
            processing_started_at=func.now(),
            attempts=Invoice.attempts + 1,
        )
        .returning(Invoice.id)
    ).first()
    db.commit()
    return claimed is not None


def store_check_flags(db: Session, invoice: Invoice, flags: list[Flag]) -> list[FieldFlag]:
    """Turn pure check results into field_flags rows (the checks never touch the DB)."""
    rows = [
        FieldFlag(
            invoice_id=invoice.id,
            code=f.code,
            field_path=f.field_path,
            reason=f.reason,
            severity=Severity(f.severity),
        )
        for f in flags
    ]
    db.add_all(rows)
    return rows


def flag_business_duplicates(db: Session, invoice: Invoice, extracted: ExtractedInvoice) -> int:
    """Same vendor + invoice number as an existing invoice -> flag for a human.

    Flag, don't reject: it might be a legit re-issue, and a human should decide.
    Returns the number of flags added.
    """
    invoice.dup_key = make_dup_key(extracted)
    if invoice.dup_key is None:
        return 0
    others = db.scalars(
        select(Invoice)
        .where(Invoice.dup_key == invoice.dup_key, Invoice.id != invoice.id)
        .order_by(Invoice.created_at)
    ).all()
    for other in others:
        other_total = (other.current_json or {}).get("total")
        db.add(
            FieldFlag(
                invoice_id=invoice.id,
                code=DUPLICATE_CODE,
                field_path="invoice_number",
                severity=Severity.error,
                reason=(
                    f"Possible duplicate of invoice {other.id} "
                    f"(same vendor and invoice number; totals: {extracted.total} vs {other_total})"
                ),
            )
        )
    return len(others)


def recover_stuck_jobs(session_factory: Callable[[], Session]) -> list[uuid.UUID]:
    """Run at server startup. Returns invoice ids that need (re)processing.

    BackgroundTasks live in server memory, so a restart/crash loses them, leaving
    invoices stuck in 'pending' or 'processing' forever. Since only one API instance
    runs (see CLAUDE.md), anything in those states at startup is orphaned.
    With multiple instances this must become a real job queue (see README: known limits).
    """
    with session_factory() as db:
        stuck = db.scalars(
            select(Invoice)
            .where(Invoice.status.in_([InvoiceStatus.pending, InvoiceStatus.processing]))
            .order_by(Invoice.created_at)
        ).all()
        to_run: list[uuid.UUID] = []
        for inv in stuck:
            if inv.attempts >= MAX_JOB_ATTEMPTS:
                log_event(log, logging.ERROR, "job_given_up", invoice_id=inv.id, attempts=inv.attempts)
                inv.status = InvoiceStatus.extraction_failed
            else:
                inv.status = InvoiceStatus.pending
                to_run.append(inv.id)
            inv.processing_started_at = None
        db.commit()
    if to_run:
        log_event(log, logging.WARNING, "stuck_jobs_recovered", count=len(to_run))
    return to_run


class DbRecorder:
    """Saves every LLM call as an extraction_runs row, committed straight away."""

    def __init__(self, db: Session, invoice: Invoice):
        self._db = db
        self._invoice = invoice
        self._rows: dict[int, ExtractionRun] = {}
        self.attempts: list[Attempt] = []  # also kept in memory, for the summary log event

    def record(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        row = ExtractionRun(invoice_id=self._invoice.id)
        self._copy(attempt, row)
        self._db.add(row)
        self._db.commit()
        self._rows[id(attempt)] = row

    def update(self, attempt: Attempt) -> None:
        self._copy(attempt, self._rows[id(attempt)])
        self._db.commit()

    @staticmethod
    def _copy(a: Attempt, row: ExtractionRun) -> None:
        row.model = a.model
        row.prompt_version = a.prompt_version
        row.raw_response = a.raw_response
        row.error = a.error
        row.latency_ms = a.latency_ms
        row.input_tokens = a.input_tokens
        row.output_tokens = a.output_tokens
        row.cost_usd = a.cost_usd


def _extract(
    db: Session, invoice: Invoice, extractor: Extractor, settings: Settings, recorder: DbRecorder
) -> ExtractedInvoice | None:
    file_bytes = Path(invoice.file_path).read_bytes()
    return extract_invoice(
        file_bytes, invoice.content_type, extractor, settings, recorder
    )
