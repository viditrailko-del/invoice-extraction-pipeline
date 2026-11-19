"""Public demo, replay mode and visitor sandboxes.

Every recorded invoice runs through the real pipeline and must end where the builder scored it. If a check, the
prompt file or the schema changes what a scenario does, these fail: re-run `python -m demo.build_replay` and
re-read the scenario's blurb.
"""

import copy
import hashlib
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Invoice, InvoiceStatus
from app.demo.replay import REPLAY_PREFIX, Scenario, replay, scenarios
from app.extraction.service import process_invoice
from app.main import app
from tests.conftest import PDF_BYTES, VALID_JSON, TestSession

ALICE = {"X-Demo-Session": "alice-sandbox-0001"}
BOB = {"X-Demo-Session": "bob-sandbox-000002"}


@pytest.fixture
def demo(client):
    base = app.dependency_overrides[get_settings]()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(update={"demo_mode": True})
    return client


def run(client, name, headers=ALICE):
    r = client.post(f"/demo/replay/{name}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def open_flag_keys(inv):
    return sorted((f["code"], f["field_path"]) for f in inv["flags"] if not f["resolved"])


# --- guards -----------------------------------------------------------------------


def test_demo_routes_are_hidden_unless_demo_mode(client):
    assert client.get("/demo/scenarios").status_code == 404
    assert client.post("/demo/replay/syn-001", headers=ALICE).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"X-Demo-Session": "short"}, {"X-Demo-Session": "bad id with spaces!!"}])
def test_demo_mode_requires_a_sandbox_id(demo, headers):
    assert demo.get("/invoices", headers=headers).status_code == 400
    assert demo.post("/demo/replay/syn-001", headers=headers).status_code == 400
    r = demo.post("/invoices", headers=headers, files={"file": ("a.pdf", PDF_BYTES, "application/pdf")})
    assert r.status_code == 400


def test_lists_scenarios_without_recordings(demo):
    listed = demo.get("/demo/scenarios").json()
    assert [s["name"] for s in listed] == list(scenarios())
    assert all("recorded" not in s and "expected" not in s for s in listed)
    assert demo.post("/demo/replay/nope", headers=ALICE).status_code == 404


# --- every scenario ----------------------------------------------------------------


@pytest.mark.parametrize("name", list(scenarios()))
def test_scenario_replays_to_its_scored_result(demo, name):
    sc = scenarios()[name]
    inv = run(demo, name)
    assert inv["status"] == sc.data["expected"]["status"]
    assert set(open_flag_keys(inv)) == {tuple(k) for k in sc.data["expected"]["flags"]}
    assert len(inv["flags"]) == sc.data["expected"]["flag_count"]
    assert inv["checks_as_of"] == sc.data["as_of"]
    assert len(inv["runs"]) == len(sc.data["recorded"]["calls"])
    assert all(r["model"] == REPLAY_PREFIX + sc.data["recorded"]["model"] for r in inv["runs"])
    assert all(r["cost_usd"] is None for r in inv["runs"])  # no Gemini call, no price


def test_scenario_files_match_their_recordings():
    for sc in scenarios().values():
        assert hashlib.sha256(sc.path.read_bytes()).hexdigest() == sc.data["recorded"]["file_sha256"], sc.name


def test_rate_limited_call_is_stored_then_retried(demo):
    inv = run(demo, "syn-015")
    first, second = inv["runs"]
    assert first["error"].startswith("429") and second["error"] is None
    assert inv["status"] == "approved"


def test_duplicate_scenario_replays_its_original_first(demo):
    inv = run(demo, "syn-025")
    [dup] = inv["flags"]
    assert (dup["code"], dup["field_path"]) == ("duplicate", "invoice_number")
    listed = demo.get("/invoices", headers=ALICE).json()
    assert sorted(i["original_filename"] for i in listed) == ["syn-001.pdf", "syn-025.jpg"]
    run(demo, "syn-025")  # the original is there now: not replayed again
    assert len(demo.get("/invoices", headers=ALICE).json()) == 2


# --- sandboxes ----------------------------------------------------------------------


def test_replaying_again_replaces_only_your_own_copy(demo, db):
    first = run(demo, "syn-019")
    run(demo, "syn-019", BOB)
    second = run(demo, "syn-019")
    assert second["id"] != first["id"]
    assert demo.get(f"/invoices/{first['id']}", headers=ALICE).status_code == 404  # replaced
    assert [i["id"] for i in demo.get("/invoices", headers=ALICE).json()] == [second["id"]]
    assert len(demo.get("/invoices", headers=BOB).json()) == 1  # Bob's copy untouched
    assert db.scalar(select(func.count()).select_from(Invoice)) == 2


def test_visitors_cannot_see_or_review_each_others_invoices(demo):
    inv = run(demo, "syn-019")
    assert demo.get(f"/invoices/{inv['id']}", headers=BOB).status_code == 404
    assert demo.get(f"/demo/invoices/{inv['id']}", headers=BOB).status_code == 404
    assert demo.get("/invoices", headers=BOB).json() == []
    body = {"corrected_by": "bob", "changes": [{"field_path": "total", "value": "622.51"}]}
    assert demo.patch(f"/invoices/{inv['id']}", json=body, headers=BOB).status_code == 404
    assert demo.post(f"/invoices/{inv['id']}/reject", headers=BOB,
                     json={"rejected_by": "bob", "reason": "x"}).status_code == 404
    assert demo.get(f"/invoices/{inv['id']}/corrections", headers=BOB).status_code == 404
    # ... while the owner can review it: fixing the total re-runs the checks and approves it.
    r = demo.patch(f"/invoices/{inv['id']}", json=body | {"corrected_by": "alice"}, headers=ALICE)
    assert r.status_code == 200 and r.json()["status"] == "approved"


def test_duplicates_are_only_detected_within_a_sandbox(demo):
    run(demo, "syn-001")
    other = run(demo, "syn-001", BOB)  # same vendor + number, another visitor
    assert other["flags"] == [] and other["status"] == "approved"


def test_same_file_upload_is_deduplicated_per_sandbox(demo):
    files = {"file": ("a.pdf", PDF_BYTES, "application/pdf")}
    a = demo.post("/invoices", headers=ALICE, files=files)
    b = demo.post("/invoices", headers=BOB, files=files)
    again = demo.post("/invoices", headers=ALICE, files=files)
    assert (a.status_code, b.status_code, again.status_code) == (201, 201, 200)
    assert a.json()["invoice"]["id"] == again.json()["invoice"]["id"] != b.json()["invoice"]["id"]


# --- replays never improvise -------------------------------------------------------


def test_a_different_input_ends_extraction_failed_not_an_improvised_answer(demo, db):
    sc = scenarios()["syn-019"]
    data = copy.deepcopy(sc.data)
    data["recorded"]["prompt_sha256"] = "0" * 64  # as if the prompt file had changed since recording
    settings = app.dependency_overrides[get_settings]()
    invoice_id = replay(db, TestSession, Scenario(sc.name, data), "carol-sandbox-0003", settings)
    inv = demo.get(f"/demo/invoices/{invoice_id}", headers={"X-Demo-Session": "carol-sandbox-0003"}).json()
    assert inv["status"] == "extraction_failed"
    assert inv["runs"][0]["error"] == "replay diverged: the prompt differs from the recording"
    assert len(inv["runs"]) == 1  # a permanent error: no retry, no fallback model


# --- pinned "today" ---------------------------------------------------------------


def test_checks_use_the_invoice_pinned_date_in_extraction_and_review(client, fake_llm, db, tmp_path):
    """VALID_JSON is dated today; pinned to 2020 it is 'in the future' at extraction and after review."""
    path = tmp_path / "x.pdf"
    path.write_bytes(PDF_BYTES)
    invoice = Invoice(file_hash=uuid.uuid4().hex, original_filename="x.pdf", content_type="application/pdf",
                      file_path=str(path), status=InvoiceStatus.pending, checks_as_of=date(2020, 1, 1))
    db.add(invoice)
    db.commit()
    fake_llm.replies = [VALID_JSON]
    process_invoice(invoice.id, TestSession, fake_llm, app.dependency_overrides[get_settings]())

    inv = client.get(f"/invoices/{invoice.id}").json()
    assert open_flag_keys(inv) == [("date_implausible", "invoice_date")]
    body = {"corrected_by": "alice", "changes": [{"field_path": "bill_to_name", "value": "Someone"}]}
    inv = client.patch(f"/invoices/{invoice.id}", json=body).json()
    assert open_flag_keys(inv) == [("date_implausible", "invoice_date")]  # re-check kept the pinned date
