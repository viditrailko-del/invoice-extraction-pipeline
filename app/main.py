import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.invoices import router as invoices_router
from app.api.review import router as review_router
from app.config import get_settings
from app.db.session import SessionLocal, get_db
from app.extraction.deps import get_extractor
from app.extraction.service import process_invoice, recover_stuck_jobs

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # On startup: re-run any invoice a previous crash/restart left behind.
    settings = get_settings()
    if settings.recover_on_startup:
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


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))  # proves the DB is reachable, not just the process
    return {"status": "ok", "db": "ok"}
