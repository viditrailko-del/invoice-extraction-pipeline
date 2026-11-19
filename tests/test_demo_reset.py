"""Public demo: the nightly reset (token-protected) and hosted DATABASE_URLs."""

from pathlib import Path

import pytest

from app.config import Settings
from app.db.models import Invoice, InvoiceStatus
from app.demo import live
from app.demo.replay import FILES_DIR
from tests.test_demo_live import ALICE, BOB, demo_mode, pdf, upload

TOKEN = "nightly-secret-token"


@pytest.fixture(autouse=True)
def _fresh_quota():
    live.quota.reset()
    yield
    live.quota.reset()


def test_reset_route_is_off_without_a_token(client):
    demo = demo_mode(client)
    assert demo.post("/demo/reset", headers={"X-Reset-Token": ""}).status_code == 404
    assert demo.post("/demo/reset", headers={"X-Reset-Token": "anything"}).status_code == 404


def test_reset_route_is_hidden_outside_demo_mode(client):
    assert client.post("/demo/reset", headers={"X-Reset-Token": TOKEN}).status_code == 404


def test_reset_needs_the_right_token(client):
    demo = demo_mode(client, demo_reset_token=TOKEN)
    assert demo.post("/demo/reset").status_code == 403
    assert demo.post("/demo/reset", headers={"X-Reset-Token": "wrong"}).status_code == 403


def test_reset_deletes_every_sandbox_and_its_uploads_but_not_the_scenarios(client, fake_llm, db):
    demo = demo_mode(client, demo_reset_token=TOKEN)
    uploaded = upload(demo, pdf(1)).json()["invoice"]
    upload(demo, pdf(2), headers=BOB)
    demo.post("/demo/replay/syn-031", headers=ALICE)
    db.add(Invoice(file_hash="outside", original_filename="x.pdf", content_type="application/pdf",
                   file_path="kept.pdf", status=InvoiceStatus.approved))  # not a demo row
    db.commit()
    stored = Path(db.get(Invoice, uploaded["id"]).file_path)
    assert stored.is_file()

    r = demo.post("/demo/reset", headers={"X-Reset-Token": TOKEN})
    assert r.status_code == 200
    assert r.json() == {"invoices_deleted": 3, "files_deleted": 2}
    db.expire_all()
    assert [i.file_hash for i in db.query(Invoice)] == ["outside"]
    assert not stored.exists()
    assert (FILES_DIR / "syn-031.jpg").is_file()  # shipped with the app, never deleted
    assert demo.get("/invoices", headers=ALICE).json() == []
    assert demo.post("/demo/replay/syn-031", headers=ALICE).status_code == 200  # still replayable


def test_reset_keeps_todays_live_count(client, fake_llm):
    demo = demo_mode(client, demo_reset_token=TOKEN, demo_live_daily_limit=1)
    assert upload(demo, pdf(1)).status_code == 201
    demo.post("/demo/reset", headers={"X-Reset-Token": TOKEN})
    assert upload(demo, pdf(2)).status_code == 429  # deleting the runs must not hand out a fresh day


@pytest.mark.parametrize("url", ["postgres://u:p@h:5432/d", "postgresql://u:p@h:5432/d?sslmode=require",
                                 "postgresql+psycopg://u:p@h:5432/d"])
def test_hosted_database_urls_use_psycopg(url):
    got = Settings(database_url=url).database_url
    assert got.startswith("postgresql+psycopg://u:p@h:5432/d")
