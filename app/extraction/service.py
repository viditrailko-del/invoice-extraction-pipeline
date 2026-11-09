"""The reliability layer around the LLM call.

process_invoice() is the whole Phase 2 job for one invoice:

  1. Call the LLM. If it's overloaded/timeouts, retry with backoff, then try the
     fallback model.                                  (transient failures)
  2. Save EVERY call to extraction_runs, raw text first, before parsing.
                                                      (never lose evidence)
  3. Validate the answer with Pydantic. If it doesn't fit the schema, retry ONCE,
     telling the model exactly what was wrong.        (bad output)
  4. Anything still failing -> status=extraction_failed. Never crash.

Until Phase 3's deterministic checks exist, nothing is auto-approved: every
successful extraction goes to needs_review.
"""

import logging
import time
import uuid
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.checks.duplicates import make_dup_key
from app.config import Settings
from app.db.models import ExtractionRun, FieldFlag, Invoice, InvoiceStatus, Severity
from app.extraction.client import Extractor, LLMError, LLMResponse
from app.schemas.extraction import ExtractedInvoice

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_TRANSIENT_RETRIES = 2  # per model, so up to 3 calls each
MAX_VALIDATION_RETRIES = 1
MAX_JOB_ATTEMPTS = 3  # a file that kills the worker 3 times is given up on

RETRY_FEEDBACK = """

Your previous answer was rejected because it did not match the required schema.
Errors:
{errors}

Return the corrected JSON for the same invoice. Follow the schema exactly."""


@lru_cache
def load_prompt(version: str) -> str:
    return (PROMPTS_DIR / f"{version}.txt").read_text(encoding="utf-8")


@lru_cache
def response_schema() -> dict:
    return ExtractedInvoice.model_json_schema()


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

        try:
            extracted = _extract(db, invoice, extractor, settings)
        except Exception:
            # Bug or unexpected error: record it, mark failed, keep the server alive.
            log.exception("extraction crashed for invoice %s", invoice_id)
            db.rollback()
            extracted = None

        if extracted is None:
            invoice.status = InvoiceStatus.extraction_failed
        else:
            invoice.extracted_json = extracted.model_dump(mode="json")  # Decimal -> exact string
            invoice.status = InvoiceStatus.needs_review  # Phase 3 decides approved vs review
            _flag_business_duplicates(db, invoice, extracted)
        invoice.processing_started_at = None
        db.commit()


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


def _flag_business_duplicates(db: Session, invoice: Invoice, extracted: ExtractedInvoice) -> None:
    """Same vendor + invoice number as an existing invoice -> flag for a human.

    Flag, don't reject: it might be a legit re-issue, and a human should decide.
    """
    invoice.dup_key = make_dup_key(extracted)
    if invoice.dup_key is None:
        return
    others = db.scalars(
        select(Invoice)
        .where(Invoice.dup_key == invoice.dup_key, Invoice.id != invoice.id)
        .order_by(Invoice.created_at)
    ).all()
    for other in others:
        other_total = (other.extracted_json or {}).get("total")
        db.add(
            FieldFlag(
                invoice_id=invoice.id,
                field_path="invoice_number",
                severity=Severity.error,
                reason=(
                    f"Possible duplicate of invoice {other.id} "
                    f"(same vendor and invoice number; totals: {extracted.total} vs {other_total})"
                ),
            )
        )


def recover_stuck_jobs(session_factory: Callable[[], Session]) -> list[uuid.UUID]:
    """Run at server startup. Returns invoice ids that need (re)processing.

    BackgroundTasks live in server memory, so a restart/crash loses them, leaving
    invoices stuck in 'pending' or 'processing' forever. Since only one API instance
    runs (see CLAUDE.md), anything in those states at startup is orphaned.
    With multiple instances this must become a real job queue (Phase 6).
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
                log.error("invoice %s gave up after %d attempts", inv.id, inv.attempts)
                inv.status = InvoiceStatus.extraction_failed
            else:
                inv.status = InvoiceStatus.pending
                to_run.append(inv.id)
            inv.processing_started_at = None
        db.commit()
    if to_run:
        log.warning("recovered %d stuck invoice(s) at startup", len(to_run))
    return to_run


def _extract(
    db: Session, invoice: Invoice, extractor: Extractor, settings: Settings
) -> ExtractedInvoice | None:
    file_bytes = Path(invoice.file_path).read_bytes()
    base_prompt = load_prompt(settings.prompt_version)
    prompt = base_prompt

    for _ in range(1 + MAX_VALIDATION_RETRIES):
        result = _call_llm(db, invoice, extractor, settings, file_bytes, prompt)
        if result is None:
            return None  # every model/attempt failed at the API level
        run, resp = result
        try:
            return ExtractedInvoice.model_validate_json(resp.text or "")
        except ValidationError as e:
            run.error = f"validation: {e}"[:4000]
            db.commit()
            prompt = base_prompt + RETRY_FEEDBACK.format(errors=_short_errors(e))
    return None


def _call_llm(
    db: Session,
    invoice: Invoice,
    extractor: Extractor,
    settings: Settings,
    file_bytes: bytes,
    prompt: str,
) -> tuple[ExtractionRun, LLMResponse] | None:
    models = [m for m in (settings.gemini_model, settings.gemini_fallback_model) if m]
    for model in models:
        for attempt in range(1 + MAX_TRANSIENT_RETRIES):
            run = ExtractionRun(
                invoice_id=invoice.id, model=model, prompt_version=settings.prompt_version
            )
            db.add(run)
            try:
                resp = extractor.generate(
                    file_bytes=file_bytes,
                    mime_type=invoice.content_type,
                    prompt=prompt,
                    schema=response_schema(),
                    model=model,
                )
            except LLMError as e:
                run.error = str(e)[:4000]
                run.latency_ms = e.latency_ms
                db.commit()
                if not e.transient:
                    break  # e.g. bad request: same model won't fix it, try the fallback
                if attempt < MAX_TRANSIENT_RETRIES:
                    time.sleep(settings.llm_retry_backoff_s * (2**attempt))
                continue

            run.raw_response = resp.text
            run.latency_ms = resp.latency_ms
            run.input_tokens = resp.input_tokens
            run.output_tokens = resp.output_tokens
            db.commit()  # raw output is saved before we even try to parse it
            return run, resp
    return None


def _short_errors(e: ValidationError) -> str:
    return "\n".join(
        f"- {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in e.errors()[:20]
    )
