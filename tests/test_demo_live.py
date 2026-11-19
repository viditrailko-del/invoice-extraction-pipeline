"""Public demo, live mode: quota, flash-lite only, generate, the document route, restarts."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

import app.api.demo as demo_api
from app.checks.runner import run_checks
from app.config import get_settings
from app.db.models import Invoice, InvoiceStatus
from app.demo import live
from app.demo.generate import PLANT_CHOICES, generate
from app.extraction.client import LLMError
from app.main import app
from app.schemas.extraction import ExtractedInvoice
from tests.conftest import PDF_BYTES, PNG_BYTES, TestSession

ALICE = {"X-Demo-Session": "alice-sandbox-0001"}
BOB = {"X-Demo-Session": "bob-sandbox-000002"}
TODAY = date(2026, 11, 19)


@pytest.fixture(autouse=True)
def _fresh_quota():
    live.quota.reset()
    yield
    live.quota.reset()


def demo_mode(client, **limits):
    base = app.dependency_overrides[get_settings]()
    settings = base.model_copy(update={"demo_mode": True, "demo_live_model": "lite-model", **limits})
    app.dependency_overrides[get_settings] = lambda: settings
    return client


@pytest.fixture
def demo(client):
    return demo_mode(client)


def upload(client, data=PDF_BYTES, headers=ALICE, name="a.pdf", ctype="application/pdf"):
    return client.post("/invoices", headers=headers, files={"file": (name, data, ctype)})


def pdf(n: int) -> bytes:
    return PDF_BYTES + f"\n% {n}".encode()


# --- quota -------------------------------------------------------------------------


def test_daily_cap_covers_upload_generate_and_retry(client, fake_llm):
    demo = demo_mode(client, demo_live_daily_limit=2, demo_live_per_ip_per_hour=99)
    assert upload(demo, pdf(1)).status_code == 201
    assert demo.post("/demo/generate", headers=ALICE, json={}).status_code == 201
    r = upload(demo, pdf(3))
    assert r.status_code == 429 and "used up" in r.json()["detail"]
    assert demo.post("/demo/generate", headers=BOB, json={}).status_code == 429
    status = demo.get("/demo/status").json()
    assert (status["live_used_today"], status["live_left_today"]) == (2, 0)


def test_per_ip_hourly_cap(client, fake_llm):
    demo = demo_mode(client, demo_live_per_ip_per_hour=2)
    assert upload(demo, pdf(1)).status_code == 201
    assert upload(demo, pdf(2), headers=BOB).status_code == 201  # same IP, another sandbox: still counted
    r = upload(demo, pdf(3))
    assert r.status_code == 429 and "per hour" in r.json()["detail"]


def test_same_file_again_and_replays_cost_nothing(client, fake_llm):
    demo = demo_mode(client, demo_live_daily_limit=1)
    assert upload(demo).status_code == 201
    assert upload(demo).status_code == 200  # idempotent: the existing record, no new extraction
    assert demo.post("/demo/replay/syn-019", headers=ALICE).status_code == 200
    assert len(fake_llm.calls) == 1
    assert demo.get("/demo/status").json()["live_used_today"] == 1


def test_retry_spends_quota(client, fake_llm):
    demo = demo_mode(client, demo_live_daily_limit=1)
    fake_llm.replies = [LLMError("bad request", transient=False)]
    inv = upload(demo).json()["invoice"]
    assert demo.get(f"/invoices/{inv['id']}", headers=ALICE).json()["status"] == "extraction_failed"
    assert demo.post(f"/invoices/{inv['id']}/retry", headers=ALICE).status_code == 429


def test_count_is_reread_from_the_db_after_a_restart(client, fake_llm):
    demo = demo_mode(client, demo_live_daily_limit=2)
    upload(demo, pdf(1))
    demo.post("/demo/replay/syn-001", headers=ALICE)  # replays are not live
    live.quota.reset()  # what a restart does to the in-memory counter
    with TestSession() as db:
        assert live.count_live_today(db, datetime.now(UTC)) == 1
        assert live.count_live_today(db, datetime.now(UTC) + timedelta(days=1)) == 0
    assert upload(demo, pdf(2)).status_code == 201
    assert upload(demo, pdf(3)).status_code == 429


# --- which model -------------------------------------------------------------------


def test_live_uses_the_lite_model_with_no_fallback(demo, fake_llm):
    fake_llm.replies = [LLMError("503 overloaded", transient=True)] * 3
    inv = upload(demo).json()["invoice"]
    assert [c["model"] for c in fake_llm.calls] == ["lite-model"] * 3  # retries, never "backup-model"
    assert demo.get(f"/invoices/{inv['id']}", headers=ALICE).json()["status"] == "extraction_failed"


def test_outside_demo_mode_nothing_changes(client, fake_llm):
    assert client.get("/demo/status").status_code == 404
    assert client.post("/demo/generate", json={}).status_code == 404
    r = client.post("/invoices", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")})
    assert r.status_code == 201
    assert fake_llm.calls[0]["model"] == "main-model"


# --- generate ------------------------------------------------------------------------


@pytest.mark.parametrize("plant", [None, *PLANT_CHOICES])
def test_generated_invoice_carries_exactly_its_planted_mistake(plant):
    g = generate(plant, TODAY, seed="fixed")
    assert g.pdf.startswith(b"%PDF")
    flags = run_checks(ExtractedInvoice.model_validate(g.printed), TODAY)
    assert sorted({f.code for f in flags}) == g.planted_codes
    assert (g.planted_codes == []) == (plant is None)
    assert generate(plant, TODAY, seed="fixed").pdf == g.pdf  # deterministic for a seed


def test_generate_reads_the_new_pdf_with_live_gemini(demo, fake_llm, monkeypatch):
    g = generate("total", TODAY, seed="fixed")
    monkeypatch.setattr(demo_api, "generate", lambda plant, today: g)
    fake_llm.replies = [json.dumps(g.printed)]  # a model that reads the page perfectly

    r = demo.post("/demo/generate", headers=ALICE, json={"plant": "total"})
    assert r.status_code == 201
    body = r.json()
    assert body["planted"] == ["total_mismatch"] and body["printed"] == g.printed
    assert body["invoice"]["status"] == "pending"
    inv = demo.get(f"/demo/invoices/{body['invoice']['id']}", headers=ALICE).json()
    assert inv["status"] == "needs_review"
    assert [f["code"] for f in inv["flags"]] == ["total_mismatch"]
    assert inv["runs"][0]["model"] == "lite-model"
    assert fake_llm.calls[0]["mime_type"] == "application/pdf"


def test_generate_rejects_unknown_plants(demo):
    assert demo.post("/demo/generate", headers=ALICE, json={"plant": "nope"}).status_code == 422


def test_status_lists_limits_and_plants(demo):
    s = demo.get("/demo/status").json()
    assert s["live_model"] == "lite-model" and s["live_left_today"] == s["live_daily_limit"]
    assert s["plants"] == PLANT_CHOICES and s["max_upload_mb"] == 1


# --- the document ---------------------------------------------------------------------


def test_document_route_serves_only_your_own_file(demo, fake_llm, tmp_path):
    inv = upload(demo, PNG_BYTES, name="x.png", ctype="image/png").json()["invoice"]
    r = demo.get(f"/demo/invoices/{inv['id']}/file", headers=ALICE)
    assert r.status_code == 200 and r.content == PNG_BYTES
    assert r.headers["content-type"] == "image/png" and r.headers["x-content-type-options"] == "nosniff"
    assert demo.get(f"/demo/invoices/{inv['id']}/file", headers=BOB).status_code == 404

    for f in (tmp_path / "uploads").iterdir():  # what a free host's restart does to the disk
        f.unlink()
    assert demo.get(f"/demo/invoices/{inv['id']}/file", headers=ALICE).status_code == 410


def test_replay_documents_ship_with_the_app(demo):
    inv = demo.post("/demo/replay/syn-011", headers=ALICE).json()
    r = demo.get(f"/demo/invoices/{inv['id']}/file", headers=ALICE)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


# --- restarts -------------------------------------------------------------------------


def test_restart_fails_interrupted_jobs_instead_of_calling_gemini(db, fake_llm):
    for n, st in enumerate([InvoiceStatus.pending, InvoiceStatus.processing, InvoiceStatus.approved]):
        db.add(Invoice(file_hash=f"h{n}", original_filename="x.pdf", content_type="application/pdf",
                       file_path="gone", status=st, demo_session="alice-sandbox-0001"))
    db.commit()
    assert live.fail_interrupted_jobs(TestSession) == 2
    db.expire_all()
    assert sorted(i.status.value for i in db.query(Invoice)) == ["approved", "extraction_failed", "extraction_failed"]
    assert fake_llm.calls == []
