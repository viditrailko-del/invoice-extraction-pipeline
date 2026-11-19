"""Demo routes. Thin: the work is in app/demo. Every route answers 404 unless DEMO_MODE is on."""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.invoices import get_owned, store_invoice
from app.config import Settings, get_settings
from app.db.models import Invoice
from app.db.session import get_db
from app.demo import live
from app.demo.generate import PLANT_CHOICES, generate
from app.demo.replay import replay, scenarios
from app.demo.reset import reset_demo
from app.logging_setup import log_event
from app.demo.sandbox import Sandbox
from app.extraction.client import Extractor
from app.extraction.deps import get_extractor, get_session_factory
from app.extraction.service import process_invoice
from app.schemas.demo import DemoInvoiceOut, DemoStatus, GenerateIn, GenerateOut


def demo_enabled(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if not settings.demo_mode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


router = APIRouter(prefix="/demo", tags=["demo"], dependencies=[Depends(demo_enabled)])
log = logging.getLogger(__name__)

Db = Annotated[Session, Depends(get_db)]
Cfg = Annotated[Settings, Depends(get_settings)]
SessionFactory = Annotated[Callable[[], Session], Depends(get_session_factory)]


def demo_invoice_out(db: Session, invoice_id: uuid.UUID) -> DemoInvoiceOut:
    invoice = db.scalar(
        select(Invoice)
        .options(selectinload(Invoice.flags), selectinload(Invoice.runs))
        .where(Invoice.id == invoice_id)
        .execution_options(populate_existing=True)  # written by another session (the extraction job)
    )
    assert invoice is not None
    out = DemoInvoiceOut.model_validate(invoice)
    out.runs.sort(key=lambda r: r.created_at)
    return out


@router.get("/scenarios")
def list_scenarios() -> list[dict[str, Any]]:
    """The recorded invoices a visitor can replay, and what each one shows."""
    return [s.summary() for s in scenarios().values()]


@router.post("/replay/{name}", response_model=DemoInvoiceOut)
def replay_scenario(name: str, sandbox: Sandbox, db: Db, settings: Cfg,
                    session_factory: SessionFactory) -> DemoInvoiceOut:
    """Run a recorded invoice through the real pipeline (validation, checks, duplicate detection) with Gemini's
    answer pre-recorded: no Gemini call. Replaces your sandbox's earlier replay of the same file."""
    sc = scenarios().get(name)
    if sc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown scenario")
    assert sandbox is not None  # demo mode always has one
    invoice_id = replay(db, session_factory, sc, sandbox, settings)
    return demo_invoice_out(db, invoice_id)


@router.get("/invoices/{invoice_id}", response_model=DemoInvoiceOut)
def get_demo_invoice(invoice_id: uuid.UUID, sandbox: Sandbox, db: Db) -> DemoInvoiceOut:
    """An invoice in your sandbox, with its LLM calls (model, tokens, time, cost)."""
    get_owned(db, invoice_id, sandbox)
    return demo_invoice_out(db, invoice_id)


@router.get("/invoices/{invoice_id}/file", responses={410: {"description": "File no longer stored"}})
def get_demo_invoice_file(invoice_id: uuid.UUID, sandbox: Sandbox, db: Db) -> FileResponse:
    """The document itself, to show beside the extraction. Uploaded and generated files live on the instance's
    disk, which a free host wipes when it restarts: then the extraction is still here but the file is gone."""
    invoice = get_owned(db, invoice_id, sandbox)
    path = Path(invoice.file_path)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "The document is no longer stored (the demo server restarted).")
    return FileResponse(path, media_type=invoice.content_type,
                        headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff",
                                 "Cache-Control": "private, max-age=3600"})


@router.get("/status", response_model=DemoStatus)
def demo_status(db: Db, settings: Cfg) -> DemoStatus:
    used = live.quota.used_today(db, datetime.now(UTC))
    return DemoStatus(
        live_model=settings.demo_live_model, live_daily_limit=settings.demo_live_daily_limit,
        live_used_today=used, live_left_today=max(settings.demo_live_daily_limit - used, 0),
        live_per_ip_per_hour=settings.demo_live_per_ip_per_hour, max_upload_mb=settings.max_upload_mb,
        plants=PLANT_CHOICES,
    )


@router.post("/reset", include_in_schema=False)
def reset(db: Db, settings: Cfg, x_reset_token: Annotated[str, Header()] = "") -> dict[str, int]:
    """Nightly (GitHub Actions): delete every visitor's invoices and files. Needs X-Reset-Token."""
    if not settings.demo_reset_token:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if not hmac.compare_digest(x_reset_token.encode(), settings.demo_reset_token.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad reset token")
    # The live quota's in-memory count is kept: today's runs are deleted here, so a re-read would hand out a fresh
    # day if this ran mid-day (the nightly run is at 00:05 UTC, right after the day rolls over anyway).
    result = reset_demo(db, settings)
    log_event(log, logging.INFO, "demo_reset", **result)
    return result


@router.post("/generate",response_model=GenerateOut, status_code=status.HTTP_201_CREATED,
             responses={429: {"description": "Live quota used up"}})
def generate_invoice(body: GenerateIn, request: Request, background: BackgroundTasks, sandbox: Sandbox, db: Db,
                     settings: Cfg, session_factory: SessionFactory,
                     extractor: Annotated[Extractor, Depends(get_extractor)]) -> GenerateOut:
    """Draw a new synthetic invoice (optionally with one planted mistake) and have real Gemini (flash-lite) read
    it. Costs one live extraction. Returns at once with status `pending`; poll GET /demo/invoices/{id}."""
    if body.plant is not None and body.plant not in PLANT_CHOICES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"plant must be one of {list(PLANT_CHOICES)}")
    run_settings = live.admit_live(db, request, settings)
    g = generate(body.plant, date.today())
    invoice, _ = store_invoice(db, g.pdf, "application/pdf", g.filename, sandbox, settings)
    background.add_task(process_invoice, invoice.id, session_factory, extractor, run_settings)
    return GenerateOut(invoice=demo_invoice_out(db, invoice.id), description=g.description, printed=g.printed,
                       planted=g.planted_codes)
