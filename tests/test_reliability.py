"""Fix #1 (no lost jobs) and fix #2 (same invoice, different file)."""

import hashlib
import json

from app.checks.duplicates import make_dup_key, normalize_vendor
from app.config import Settings
from app.db.models import Invoice, InvoiceStatus
from app.extraction.service import MAX_JOB_ATTEMPTS, process_invoice, recover_stuck_jobs
from app.schemas.extraction import ExtractedInvoice
from tests.conftest import PDF_BYTES, VALID_JSON, TestSession

SETTINGS = Settings(
    gemini_api_key="fake", gemini_model="m", gemini_fallback_model="", llm_retry_backoff_s=0
)


def make_json(**overrides) -> str:
    data = json.loads(VALID_JSON)
    vendor = overrides.pop("vendor", None)
    if vendor is not None:
        data["vendor"]["name"] = vendor
    data.update(overrides)
    return json.dumps(data)


def upload(client, data: bytes) -> str:
    r = client.post("/invoices", files={"file": ("inv.pdf", data, "application/pdf")})
    return r.json()["invoice"]["id"]


def flags(client, inv_id):
    return client.get(f"/invoices/{inv_id}").json()["flags"]


def add_invoice(db, tmp_path, status: InvoiceStatus, attempts: int = 0) -> Invoice:
    data = PDF_BYTES + str(len(list(tmp_path.iterdir()))).encode()
    path = tmp_path / f"{hashlib.sha256(data).hexdigest()}.pdf"
    path.write_bytes(data)
    inv = Invoice(
        file_hash=hashlib.sha256(data).hexdigest(),
        original_filename="x.pdf",
        content_type="application/pdf",
        file_path=str(path),
        status=status,
        attempts=attempts,
    )
    db.add(inv)
    db.commit()
    return inv


# ===== Fix #1: jobs survive a crash / restart ================================


def test_recovery_requeues_pending_and_processing(db, tmp_path):
    stuck_pending = add_invoice(db, tmp_path, InvoiceStatus.pending)
    stuck_midway = add_invoice(db, tmp_path, InvoiceStatus.processing, attempts=1)
    done = add_invoice(db, tmp_path, InvoiceStatus.needs_review, attempts=1)

    ids = recover_stuck_jobs(TestSession)

    assert set(ids) == {stuck_pending.id, stuck_midway.id}
    db.expire_all()
    assert stuck_midway.status == InvoiceStatus.pending
    assert done.status == InvoiceStatus.needs_review  # finished work is left alone


def test_recovered_job_actually_completes(db, tmp_path, fake_llm):
    inv = add_invoice(db, tmp_path, InvoiceStatus.processing, attempts=1)
    for invoice_id in recover_stuck_jobs(TestSession):
        process_invoice(invoice_id, TestSession, fake_llm, SETTINGS)
    db.expire_all()
    assert inv.status == InvoiceStatus.needs_review
    assert inv.extracted_json["invoice_number"] == "INV-1"
    assert inv.attempts == 2


def test_poison_file_given_up_after_max_attempts(db, tmp_path):
    # This file has already "crashed the server" MAX times: stop retrying it.
    inv = add_invoice(db, tmp_path, InvoiceStatus.processing, attempts=MAX_JOB_ATTEMPTS)
    assert recover_stuck_jobs(TestSession) == []
    db.expire_all()
    assert inv.status == InvoiceStatus.extraction_failed


def test_job_cannot_run_twice(db, tmp_path, fake_llm):
    inv = add_invoice(db, tmp_path, InvoiceStatus.pending)
    process_invoice(inv.id, TestSession, fake_llm, SETTINGS)
    process_invoice(inv.id, TestSession, fake_llm, SETTINGS)  # e.g. recovery + upload race
    assert len(fake_llm.calls) == 1


def test_claimed_job_is_skipped(db, tmp_path, fake_llm):
    inv = add_invoice(db, tmp_path, InvoiceStatus.processing, attempts=1)
    process_invoice(inv.id, TestSession, fake_llm, SETTINGS)  # another worker owns it
    assert fake_llm.calls == []


def test_manual_retry_resets_attempts(client, fake_llm, db):
    from tests.test_extraction import overloaded

    fake_llm.replies = [overloaded()] * 6
    inv_id = upload(client, PDF_BYTES)
    client.post(f"/invoices/{inv_id}/retry")
    assert db.get(Invoice, inv_id).attempts == 1


# ===== Fix #2: same invoice, different file ==================================


def test_rescan_of_same_invoice_is_flagged(client, fake_llm):
    first = upload(client, PDF_BYTES)
    second = upload(client, PDF_BYTES + b"rescanned")  # different bytes, same invoice
    assert flags(client, first) == []
    [flag] = flags(client, second)
    assert flag["field_path"] == "invoice_number"
    assert flag["severity"] == "error"
    assert first in flag["reason"]
    assert client.get(f"/invoices/{second}").json()["status"] == "needs_review"


def test_formatting_differences_still_match(client, fake_llm):
    fake_llm.replies = [
        make_json(invoice_number="NW-2026-0417", vendor="Northwind Cloud Services Ltd."),
        make_json(invoice_number="nw 2026 0417", vendor="NORTHWIND CLOUD SERVICES LIMITED"),
    ]
    upload(client, PDF_BYTES)
    second = upload(client, PDF_BYTES + b"2")
    assert len(flags(client, second)) == 1


def test_different_invoice_number_not_flagged(client, fake_llm):
    fake_llm.replies = [make_json(invoice_number="INV-1"), make_json(invoice_number="INV-2")]
    upload(client, PDF_BYTES)
    assert flags(client, upload(client, PDF_BYTES + b"2")) == []


def test_same_number_different_vendor_not_flagged(client, fake_llm):
    fake_llm.replies = [make_json(vendor="Acme"), make_json(vendor="Globex")]
    upload(client, PDF_BYTES)
    assert flags(client, upload(client, PDF_BYTES + b"2")) == []


def test_reason_shows_both_totals(client, fake_llm):
    # Same invoice number but different total: maybe a corrected re-issue. Human decides.
    fake_llm.replies = [make_json(total="22.00"), make_json(total="25.00")]
    upload(client, PDF_BYTES)
    [flag] = flags(client, upload(client, PDF_BYTES + b"2"))
    assert "25.00 vs 22.00" in flag["reason"]


def test_third_copy_flags_both_earlier_ones(client):
    upload(client, PDF_BYTES)
    upload(client, PDF_BYTES + b"2")
    assert len(flags(client, upload(client, PDF_BYTES + b"3"))) == 2


# --- the pure key function ---------------------------------------------------


def test_no_key_without_invoice_number():
    assert make_dup_key(ExtractedInvoice.model_validate({"vendor": {"name": "Acme"}})) is None


def test_no_key_without_vendor():
    assert make_dup_key(ExtractedInvoice.model_validate({"invoice_number": "1"})) is None


def test_tax_id_used_when_no_vendor_name():
    inv = ExtractedInvoice.model_validate({"invoice_number": "1", "vendor": {"tax_id": "GB 123"}})
    assert make_dup_key(inv) == "tax:gb123|1"


def test_vendor_suffixes_ignored():
    assert normalize_vendor("Acme Pvt. Ltd.") == normalize_vendor("ACME") == "acme"
    assert normalize_vendor("Coca Cola Co") == "cocacola"
    assert normalize_vendor("Acme Analytics") != normalize_vendor("Acme")
