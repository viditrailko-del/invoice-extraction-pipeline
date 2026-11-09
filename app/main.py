from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.invoices import router as invoices_router
from app.db.session import get_db

app = FastAPI(title="Invoice Extraction Pipeline", version="0.1.0")
app.include_router(invoices_router)


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))  # proves the DB is reachable, not just the process
    return {"status": "ok", "db": "ok"}
