import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.demo import router as demo_router
from app.api.invoices import router as invoices_router
from app.api.review import router as review_router
from app.config import Settings, get_settings
from app.db.session import SessionLocal, get_db
from app.demo.live import fail_interrupted_jobs
from app.extraction.deps import get_extractor
from app.extraction.service import process_invoice, recover_stuck_jobs
from app.logging_setup import adopt_uvicorn_loggers, configure_logging, log_event

configure_logging(get_settings().log_format, get_settings().log_level)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # On startup: re-run any invoice a previous crash/restart left behind.
    settings = get_settings()
    adopt_uvicorn_loggers()  # uvicorn configured its loggers after our import
    if not settings.gemini_api_key:
        log_event(log, logging.ERROR, "gemini_api_key_missing",
                  hint="uploads are stored but every extraction will fail until it is set")
    if settings.recover_on_startup and settings.demo_mode:
        failed = await asyncio.get_running_loop().run_in_executor(None, fail_interrupted_jobs, SessionLocal)
        if failed:
            log_event(log, logging.WARNING, "demo_interrupted_jobs_failed", count=failed)
    elif settings.recover_on_startup:
        loop = asyncio.get_running_loop()
        try:
            ids = await loop.run_in_executor(None, recover_stuck_jobs, SessionLocal)
            extractor = get_extractor(settings)
            for invoice_id in ids:
                # Runs in a worker thread; doesn't block the server from starting.
                loop.run_in_executor(
                    None, process_invoice, invoice_id, SessionLocal, extractor, settings
                )
        except Exception:
            log.exception("startup recovery failed; server starting anyway")
    yield


app = FastAPI(title="Invoice Extraction Pipeline", version="0.2.0", lifespan=lifespan)
app.include_router(invoices_router)
app.include_router(review_router)
app.include_router(demo_router)


DEMO_PAGE = Path(__file__).parent / "static" / "demo.html"


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False, response_model=None)
def home(settings: Annotated[Settings, Depends(get_settings)]) -> FileResponse | RedirectResponse:
    """The demo page on the public demo; elsewhere the API docs."""
    if settings.demo_mode:
        return FileResponse(DEMO_PAGE, media_type="text/html")
    return RedirectResponse("/docs")


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))  # proves the DB is reachable, not just the process
    return {"status": "ok", "db": "ok"}
