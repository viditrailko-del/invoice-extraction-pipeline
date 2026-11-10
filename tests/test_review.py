"""Phase 4: human review — corrections with audit trail, resolving flags, rejecting."""

import json
import uuid

from app.extraction.client import LLMError
from tests.conftest import PDF_BYTES, VALID_JSON


def llm_json(**overrides) -> str:
    data = json.loads(VALID_JSON)
    data.update(overrides)
    return json.dumps(data)


def upload(client, data: bytes = PDF_BYTES) -> str:
    r = client.post("/invoices", files={"file": ("inv.pdf", data, "application/pdf")})
    return r.json()["invoice"]["id"]


def get(client, inv_id):
    return client.get(f"/invoices/{inv_id}").json()


def patch(client, inv_id, *changes, by="alice", note=None):
    body = {
        "corrected_by": by,
        "note": note,
        "changes": [{"field_path": p, "value": v} for p, v in changes],
    }
    return client.patch(f"/invoices/{inv_id}", json=body)


def corrections(client, inv_id):
    return client.get(f"/invoices/{inv_id}/corrections").json()


def open_flags(inv):
    return [f for f in inv["flags"] if not f["resolved"]]


def bad_total_invoice(client, fake_llm) -> str:
    fake_llm.replies = [llm_json(total="99.00")]  # 20.00 + 2.00 != 99.00
    return upload(client)


# --- corrections ----------------------------------------------------------------


def test_correction_fixes_problem_and_approves(client, fake_llm):
    inv_id = bad_total_invoice(client, fake_llm)
    r = patch(client, inv_id, ("total", "22.00"), note="misread 22 as 99")
    assert r.status_code == 200
    inv = r.json()

    assert inv["status"] == "approved"
    assert inv["corrected_json"]["total"] == "22.00"
    assert inv["extracted_json"]["total"] == "99.00"  # LLM answer is never overwritten
    [flag] = inv["flags"]  # kept, not deleted
    assert flag["code"] == "total_mismatch"
    assert flag["resolved"] is True
    assert flag["resolved_by"] == "system:recheck"

    [c] = corrections(client, inv_id)
    assert (c["field_path"], c["old_value"], c["new_value"]) == ("total", "99.00", "22.00")
    assert c["corrected_by"] == "alice"
    assert c["note"] == "misread 22 as 99"


def test_wrong_correction_replaces_flag_with_current_numbers(client, fake_llm):
    inv_id = bad_total_invoice(client, fake_llm)
    inv = patch(client, inv_id, ("total", "50.00")).json()
    assert inv["status"] == "needs_review"
    [still_open] = open_flags(inv)
    assert "50.00" in still_open["reason"]  # reason shows the current value, not 99.00
    assert len(inv["flags"]) == 2  # old one resolved, history kept


def test_correction_can_break_an_approved_invoice(client):
    inv_id = upload(client)
    assert get(client, inv_id)["status"] == "approved"
    inv = patch(client, inv_id, ("line_items[0].amount", "30.00")).json()
    assert inv["status"] == "needs_review"
    assert {f["code"] for f in open_flags(inv)} == {"line_math", "subtotal_mismatch"}


def test_several_changes_in_one_patch(client, fake_llm):
    fake_llm.replies = [llm_json(invoice_number=None, currency=None)]
    inv_id = upload(client)
    inv = patch(client, inv_id, ("invoice_number", "INV-1"), ("currency", "USD")).json()
    assert inv["status"] == "approved"
    assert [c["field_path"] for c in corrections(client, inv_id)] == ["invoice_number", "currency"]


def test_replace_whole_line_items_list(client, fake_llm):
    fake_llm.replies = [llm_json(subtotal="30.00", total="32.00")]  # LLM missed a line
    inv_id = upload(client)
    lines = [
        {"description": "Widget", "quantity": "2", "unit_price": "10.00", "amount": "20.00"},
        {"description": "Gadget", "quantity": "1", "unit_price": "10.00", "amount": "10.00"},
    ]
    inv = patch(client, inv_id, ("line_items", lines)).json()
    assert inv["status"] == "approved"
    assert len(inv["corrected_json"]["line_items"]) == 2


def test_second_correction_builds_on_the_first(client, fake_llm):
    fake_llm.replies = [llm_json(invoice_number=None, currency=None)]
    inv_id = upload(client)
    patch(client, inv_id, ("invoice_number", "INV-1"))
    inv = patch(client, inv_id, ("currency", "USD")).json()
    assert inv["corrected_json"]["invoice_number"] == "INV-1"  # not lost
    assert inv["status"] == "approved"


def test_same_value_is_not_a_correction(client, fake_llm):
    inv_id = bad_total_invoice(client, fake_llm)
    inv = patch(client, inv_id, ("total", "99")).json()  # == 99.00 as money
    assert corrections(client, inv_id) == []
    assert inv["corrected_json"] is None
    assert inv["status"] == "needs_review"


def test_invalid_value_rejected_and_nothing_saved(client, fake_llm):
    inv_id = bad_total_invoice(client, fake_llm)
    # first change is fine, second is invalid: all-or-nothing
    r = patch(client, inv_id, ("total", "22.00"), ("currency", "dollars"))
    assert r.status_code == 422
    assert "currency" in str(r.json()["detail"])
    assert corrections(client, inv_id) == []
    assert get(client, inv_id)["corrected_json"] is None


def test_unknown_or_malformed_path_rejected(client):
    inv_id = upload(client)
    assert patch(client, inv_id, ("vendor.nickname", "x")).status_code == 422
    assert patch(client, inv_id, ("line_items[9].amount", "1")).status_code == 422
    assert patch(client, inv_id, ("total..x", "1")).status_code == 422


def test_request_validation(client):
    inv_id = upload(client)
    assert patch(client, inv_id, ("total", "1"), by="  ").status_code == 422  # blank name
    r = client.patch(f"/invoices/{inv_id}", json={"corrected_by": "a", "changes": []})
    assert r.status_code == 422


def test_cannot_correct_failed_or_missing_invoice(client, fake_llm):
    fake_llm.replies = [LLMError("503", transient=True)] * 6
    inv_id = upload(client)
    assert patch(client, inv_id, ("total", "1")).status_code == 409
    assert patch(client, str(uuid.uuid4()), ("total", "1")).status_code == 404


# --- resolving flags ------------------------------------------------------------


def test_human_resolves_warning_and_invoice_approves(client, fake_llm):
    fake_llm.replies = [llm_json(invoice_date="2020-01-01")]  # old: warning
    inv_id = upload(client)
    [flag] = get(client, inv_id)["flags"]
    r = client.post(
        f"/invoices/{inv_id}/flags/{flag['id']}/resolve",
        json={"resolved_by": "bob", "note": "old contract, date is right"},
    )
    assert r.status_code == 200
    inv = r.json()
    assert inv["status"] == "approved"
    [resolved] = inv["flags"]
    assert (resolved["resolved_by"], resolved["resolution_note"]) == ("bob", "old contract, date is right")
    assert resolved["resolved_at"] is not None

    # resolving twice is a conflict
    again = client.post(f"/invoices/{inv_id}/flags/{flag['id']}/resolve", json={"resolved_by": "bob"})
    assert again.status_code == 409


def test_accepted_flag_not_raised_again_after_other_correction(client, fake_llm):
    fake_llm.replies = [llm_json(invoice_date="2020-01-01")]
    inv_id = upload(client)
    [flag] = get(client, inv_id)["flags"]
    client.post(f"/invoices/{inv_id}/flags/{flag['id']}/resolve", json={"resolved_by": "bob"})
    inv = patch(client, inv_id, ("bill_to_name", "Buyer Ltd")).json()
    assert inv["status"] == "approved"
    assert len(inv["flags"]) == 1  # the human's decision stands


def test_resolve_flag_of_another_invoice_is_404(client, fake_llm):
    first = bad_total_invoice(client, fake_llm)
    [flag] = get(client, first)["flags"]
    other = upload(client, PDF_BYTES + b"other")
    r = client.post(f"/invoices/{other}/flags/{flag['id']}/resolve", json={"resolved_by": "bob"})
    assert r.status_code == 404


def test_one_resolved_of_two_keeps_review(client, fake_llm):
    fake_llm.replies = [llm_json(invoice_date="2020-01-01", total="99.00")]
    inv_id = upload(client)
    warning = next(f for f in get(client, inv_id)["flags"] if f["severity"] == "warning")
    inv = client.post(
        f"/invoices/{inv_id}/flags/{warning['id']}/resolve", json={"resolved_by": "bob"}
    ).json()
    assert inv["status"] == "needs_review"  # total error still open


# --- duplicates + reject --------------------------------------------------------


def test_confirmed_duplicate_is_rejected_with_audit(client):
    upload(client)
    second = upload(client, PDF_BYTES + b"rescan")
    assert get(client, second)["status"] == "needs_review"

    r = client.post(
        f"/invoices/{second}/reject", json={"rejected_by": "carol", "reason": "duplicate scan"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    [c] = corrections(client, second)
    assert (c["field_path"], c["old_value"], c["new_value"]) == ("status", "needs_review", "rejected")
    assert (c["corrected_by"], c["note"]) == ("carol", "duplicate scan")

    # rejected is final
    assert patch(client, second, ("total", "1")).status_code == 409
    assert client.post(f"/invoices/{second}/reject", json={"rejected_by": "c", "reason": "x"}).status_code == 409


def test_reject_needs_a_reason(client):
    inv_id = upload(client)
    r = client.post(f"/invoices/{inv_id}/reject", json={"rejected_by": "carol", "reason": " "})
    assert r.status_code == 422


def test_failed_extraction_can_be_rejected(client, fake_llm):
    fake_llm.replies = [LLMError("503", transient=True)] * 6
    inv_id = upload(client)
    r = client.post(f"/invoices/{inv_id}/reject", json={"rejected_by": "c", "reason": "not an invoice"})
    assert r.json()["status"] == "rejected"


def test_correcting_invoice_number_clears_duplicate_flag(client, fake_llm):
    upload(client)
    second = upload(client, PDF_BYTES + b"rescan")  # LLM misread: same number as first
    inv = patch(client, second, ("invoice_number", "INV-2")).json()
    assert inv["status"] == "approved"
    [dup] = inv["flags"]
    assert dup["code"] == "duplicate"
    assert dup["resolved_by"] == "system:recheck"


def test_correction_can_create_a_duplicate(client, fake_llm):
    upload(client)  # INV-1
    fake_llm.replies = [llm_json(invoice_number="INV-7")]
    second = upload(client, PDF_BYTES + b"2")
    assert get(client, second)["status"] == "approved"
    inv = patch(client, second, ("invoice_number", "INV-1")).json()
    assert inv["status"] == "needs_review"
    assert [f["code"] for f in open_flags(inv)] == ["duplicate"]


# --- audit trail endpoint ---------------------------------------------------------


def test_corrections_endpoint_404(client):
    assert client.get(f"/invoices/{uuid.uuid4()}/corrections").status_code == 404
