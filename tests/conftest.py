"""Test setup: a separate `invoices_test` database, wiped between tests.

Tests NEVER touch the dev database or call real Gemini.
"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.models import Base
from app.db.session import get_db
from app.main import app

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
def client(tmp_path) -> Iterator[TestClient]:
    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url=TEST_DB_URL, upload_dir=tmp_path / "uploads", max_upload_mb=1
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


PDF_BYTES = b"%PDF-1.4\n% fake invoice for tests\n%%EOF"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
