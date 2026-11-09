import hashlib
import uuid

from tests.conftest import PDF_BYTES, PNG_BYTES


def upload(client, data=PDF_BYTES, name="inv.pdf", ctype="application/pdf"):
    return client.post("/invoices", files={"file": (name, data, ctype)})


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "db": "ok"}


def test_upload_creates_pending_invoice(client):
    r = upload(client)
    assert r.status_code == 201
    body = r.json()
    assert body["duplicate"] is False
    inv = body["invoice"]
    assert inv["status"] == "pending"
    assert inv["original_filename"] == "inv.pdf"
    assert inv["file_hash"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert inv["extracted_json"] is None
    assert inv["flags"] == []


def test_upload_stores_file_by_hash(client, tmp_path):
    upload(client)
    stored = tmp_path / "uploads" / f"{hashlib.sha256(PDF_BYTES).hexdigest()}.pdf"
    assert stored.read_bytes() == PDF_BYTES


def test_duplicate_upload_is_idempotent(client):
    first = upload(client).json()["invoice"]
    r = upload(client, name="renamed.pdf")
    assert r.status_code == 200
    assert r.json()["duplicate"] is True
    assert r.json()["invoice"]["id"] == first["id"]
    assert len(client.get("/invoices").json()) == 1


def test_png_accepted(client):
    assert upload(client, PNG_BYTES, "inv.png", "image/png").status_code == 201


def test_rejects_unsupported_type(client):
    assert upload(client, b"hello", "x.txt", "text/plain").status_code == 415


def test_rejects_content_type_mismatch(client):
    # Claims PDF but bytes aren't a PDF.
    assert upload(client, b"not a pdf", "x.pdf", "application/pdf").status_code == 400


def test_rejects_empty_file(client):
    assert upload(client, b"").status_code == 400


def test_rejects_too_large(client):
    big = b"%PDF" + b"0" * (1024 * 1024 + 1)  # limit is 1 MB in tests
    assert upload(client, big).status_code == 413


def test_get_invoice(client):
    inv_id = upload(client).json()["invoice"]["id"]
    r = client.get(f"/invoices/{inv_id}")
    assert r.status_code == 200
    assert r.json()["id"] == inv_id


def test_get_missing_invoice_404(client):
    assert client.get(f"/invoices/{uuid.uuid4()}").status_code == 404


def test_list_filters_by_status(client):
    upload(client)  # fake LLM succeeds -> needs_review
    assert len(client.get("/invoices", params={"status": "needs_review"}).json()) == 1
    assert client.get("/invoices", params={"status": "approved"}).json() == []
