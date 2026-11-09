"""Test setup: a separate `invoices_test` database, wiped between tests.

Tests NEVER touch the dev database or call real Gemini.
"""

import os

# Must be set BEFORE importing the app: startup recovery would otherwise touch the
# dev database and call real Gemini. Recovery is tested directly instead.
os.environ["RECOVER_ON_STARTUP"] = "false"

from collections.abc import Iterator  # noqa: E402

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.models import Base
from app.db.session import get_db
from app.extraction.client import LLMError, LLMResponse
from app.extraction.deps import get_extractor, get_session_factory
from app.main import app

VALID_JSON = """{
  "invoice_number": "INV-1", "invoice_date": "2026-11-03", "due_date": null,
  "vendor": {"name": "Acme", "address": null, "tax_id": null},
  "bill_to_name": "Buyer", "currency": "USD",
  "line_items": [{"description": "Widget", "quantity": 2, "unit_price": "10.00", "amount": "20.00"}],
  "subtotal": "20.00", "tax": "2.00", "discount": null, "total": "22.00"
}"""


class FakeExtractor:
    """Stands in for Gemini. Each call pops the next scripted reply:
    a str (returned as the model's text) or an LLMError (raised)."""

    def __init__(self) -> None:
        self.replies: list[str | LLMError] = []
        self.calls: list[dict] = []

    def generate(self, *, file_bytes, mime_type, prompt, schema, model) -> LLMResponse:
        self.calls.append({"model": model, "prompt": prompt, "mime_type": mime_type})
        reply = self.replies.pop(0) if self.replies else VALID_JSON
        if isinstance(reply, LLMError):
            raise reply
        return LLMResponse(text=reply, model=model, latency_ms=5, input_tokens=100, output_tokens=50)

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://invoice:invoice@localhost:5432/invoices_test"
)

engine = create_engine(TEST_DB_URL)
TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    yield
    with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def fake_llm() -> FakeExtractor:
    return FakeExtractor()


@pytest.fixture
def client(tmp_path, fake_llm) -> Iterator[TestClient]:
    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url=TEST_DB_URL,
        upload_dir=tmp_path / "uploads",
        max_upload_mb=1,
        gemini_api_key="fake",
        gemini_model="main-model",
        gemini_fallback_model="backup-model",
        llm_retry_backoff_s=0,  # don't actually sleep in tests
    )
    app.dependency_overrides[get_extractor] = lambda: fake_llm
    app.dependency_overrides[get_session_factory] = lambda: TestSession
    # TestClient runs background tasks before returning, so tests see the final state.
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def db() -> Iterator[Session]:
    with TestSession() as s:
        yield s


PDF_BYTES = b"%PDF-1.4\n% fake invoice for tests\n%%EOF"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
