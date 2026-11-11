"""The reliability layer: what happens when Gemini is right, wrong, slow, or down."""

import json
from decimal import Decimal

from sqlalchemy import select

from app.db.models import ExtractionRun
from app.extraction.client import LLMError
from app.schemas.extraction import ExtractedInvoice
from tests.conftest import PDF_BYTES, VALID_JSON


def upload(client):
    r = client.post("/invoices", files={"file": ("inv.pdf", PDF_BYTES, "application/pdf")})
    return r.json()["invoice"]["id"]


def get(client, inv_id):
    return client.get(f"/invoices/{inv_id}").json()


def runs(db):
    return db.scalars(select(ExtractionRun).order_by(ExtractionRun.created_at)).all()


def overloaded():
    return LLMError("503: overloaded", transient=True)


# --- happy path -------------------------------------------------------------


def test_clean_extraction_is_auto_approved(client, fake_llm, db):
    inv = get(client, upload(client))
    assert inv["status"] == "approved"
    assert inv["extracted_json"]["invoice_number"] == "INV-1"
    assert len(fake_llm.calls) == 1
    assert fake_llm.calls[0]["model"] == "main-model"
    assert fake_llm.calls[0]["mime_type"] == "application/pdf"


def test_money_stored_as_exact_strings(client):
    inv = get(client, upload(client))
    # Decimal -> "22.00", never a float like 22.000000001
    assert inv["extracted_json"]["total"] == "22.00"
    assert inv["extracted_json"]["line_items"][0]["unit_price"] == "10.00"


def test_raw_response_saved_with_metadata(client, db):
    upload(client)
    [run] = runs(db)
    assert run.raw_response == VALID_JSON
    assert run.model == "main-model"
    assert run.prompt_version == "v3"
    assert run.input_tokens == 100 and run.output_tokens == 50
    assert run.error is None


def test_duplicate_upload_does_not_call_llm_again(client, fake_llm):
    upload(client)
    upload(client)
    assert len(fake_llm.calls) == 1


# --- bad output from the model ----------------------------------------------


def test_invalid_json_retried_once_with_feedback(client, fake_llm, db):
    fake_llm.replies = ['{"total": "not a number"}', VALID_JSON]
    inv = get(client, upload(client))
    assert inv["status"] == "approved"
    assert len(fake_llm.calls) == 2
    assert "previous answer was rejected" in fake_llm.calls[1]["prompt"]
    assert "total" in fake_llm.calls[1]["prompt"]  # tells the model WHICH field was wrong
    first, second = runs(db)
    assert first.error.startswith("validation:")
    assert first.raw_response == '{"total": "not a number"}'  # bad output still kept
    assert second.error is None


def test_invalid_twice_marks_failed(client, fake_llm):
    fake_llm.replies = ["not json at all", '{"unknown_field": 1}']
    inv = get(client, upload(client))
    assert inv["status"] == "extraction_failed"
    assert inv["extracted_json"] is None
    assert len(fake_llm.calls) == 2


def test_extra_fields_rejected(client, fake_llm):
    # The model inventing fields we didn't ask for is a schema violation.
    fake_llm.replies = ['{"hallucinated": "x"}', '{"hallucinated": "x"}']
    assert get(client, upload(client))["status"] == "extraction_failed"


# --- the API being down / overloaded ----------------------------------------


def test_transient_error_retried_on_same_model(client, fake_llm, db):
    fake_llm.replies = [overloaded(), VALID_JSON]
    inv = get(client, upload(client))
    assert inv["status"] == "approved"
    assert [c["model"] for c in fake_llm.calls] == ["main-model", "main-model"]
    assert runs(db)[0].error == "503: overloaded"  # the failure is recorded too


def test_falls_back_to_backup_model(client, fake_llm):
    fake_llm.replies = [overloaded(), overloaded(), overloaded(), VALID_JSON]
    inv = get(client, upload(client))
    assert inv["status"] == "approved"
    assert [c["model"] for c in fake_llm.calls] == ["main-model"] * 3 + ["backup-model"]


def test_permanent_error_skips_retries_and_goes_to_backup(client, fake_llm):
    fake_llm.replies = [LLMError("400: bad request", transient=False), VALID_JSON]
    inv = get(client, upload(client))
    assert inv["status"] == "approved"
    assert [c["model"] for c in fake_llm.calls] == ["main-model", "backup-model"]


def test_everything_down_marks_failed_without_crashing(client, fake_llm, db):
    fake_llm.replies = [overloaded()] * 6
    inv = get(client, upload(client))
    assert inv["status"] == "extraction_failed"
    assert len(runs(db)) == 6  # every attempt is in the audit trail


def test_unexpected_crash_marks_failed(client, fake_llm):
    def crash(**_):
        raise RuntimeError("bug!")  # not an LLMError: simulates a bug in our code

    fake_llm.generate = crash
    assert get(client, upload(client))["status"] == "extraction_failed"


# --- retry endpoint ---------------------------------------------------------


def test_retry_failed_invoice(client, fake_llm):
    fake_llm.replies = [overloaded()] * 6
    inv_id = upload(client)
    assert get(client, inv_id)["status"] == "extraction_failed"

    r = client.post(f"/invoices/{inv_id}/retry")  # Gemini is back: default reply is valid
    assert r.status_code == 202
    assert get(client, inv_id)["status"] == "approved"


def test_retry_rejected_unless_failed(client):
    inv_id = upload(client)  # succeeds -> approved
    assert client.post(f"/invoices/{inv_id}/retry").status_code == 409


# --- schema ------------------------------------------------------------------


def test_schema_parses_money_exactly():
    inv = ExtractedInvoice.model_validate_json(VALID_JSON)
    assert inv.total == Decimal("22.00")
    assert inv.line_items[0].quantity == 2


def test_schema_rejects_bad_currency():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExtractedInvoice.model_validate({"currency": "dollars"})


# --- Phase 3: checks decide approved vs needs_review --------------------------


def with_changes(**overrides) -> str:
    data = json.loads(VALID_JSON)
    data.update(overrides)
    return json.dumps(data)


def test_clean_extraction_has_no_flags(client):
    assert get(client, upload(client))["flags"] == []


def test_failed_check_sends_to_review_with_stored_flag(client, fake_llm):
    fake_llm.replies = [with_changes(total="99.00")]  # 20.00 + 2.00 != 99.00
    inv = get(client, upload(client))
    assert inv["status"] == "needs_review"
    [flag] = inv["flags"]
    assert flag["field_path"] == "total"
    assert flag["severity"] == "error"
    assert flag["resolved"] is False


def test_warning_alone_still_blocks_approval(client, fake_llm):
    fake_llm.replies = [with_changes(invoice_date="2000-01-01")]  # too old: warning only
    inv = get(client, upload(client))
    assert inv["status"] == "needs_review"
    assert [f["severity"] for f in inv["flags"]] == ["warning"]


def test_several_problems_give_several_flags(client, fake_llm):
    fake_llm.replies = [with_changes(invoice_number=None, currency=None, subtotal="5.00")]
    paths = {f["field_path"] for f in get(client, upload(client))["flags"]}
    # subtotal 5.00 also breaks the total check (5.00 + 2.00 != 22.00)
    assert paths == {"invoice_number", "currency", "subtotal", "total"}
