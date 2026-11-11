"""The eval harness's own logic: scoring rules, cost, and the extract -> score plumbing.

Never calls Gemini: the extract step is driven by the FakeExtractor.
"""

import json
from decimal import Decimal

import pytest

from app.checks.duplicates import DUPLICATE_CODE
from app.extraction.pricing import cost_usd
from eval.dataset import load_fixtures
from eval.report import build_report
from eval.run import MemoryRecorder, extract_one, score_run
from eval.scoring import (
    decide,
    get_path,
    score_field,
    score_fields,
    score_flags,
    values_match,
)
from tests.conftest import VALID_JSON

# --- pricing ------------------------------------------------------------------


def test_cost_counts_thinking_tokens_as_output():
    # flash-lite: $0.30 in, $2.50 out per 1M tokens
    assert cost_usd("gemini-3.5-flash-lite", 1_000_000, 0) == Decimal("0.300000")
    assert cost_usd("gemini-3.5-flash-lite", 0, 600_000, 400_000) == Decimal("2.500000")


def test_unknown_model_cost_is_none_not_zero():
    assert cost_usd("some-new-model", 100, 50) is None
    assert cost_usd("gemini-3.5-flash-lite", None, 50) is None


# --- field matching -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected", "actual", "match"),
    [
        ("total", "22.00", 22, True),  # number vs string, same value
        ("total", "22.00", "22.01", True),  # within the 1-cent tolerance
        ("total", "22.00", "22.02", False),
        ("discount", "-107.48", "107.48", True),  # printed sign doesn't matter
        ("line_items[0].quantity", "9.5", 9.5, True),
        ("total", "22.00", "abc", False),
        ("vendor.name", "Northwind Cloud Services Ltd.", "NORTHWIND cloud services ltd", True),
        ("vendor.address", "RUA JOÃO DE RUÃO", "Rua Joao de Ruao", True),  # accents
        ("vendor.name", "Acme", "Acme Corp", False),
        ("invoice_date", "2026-10-31", "2026-10-31", True),
        ("invoice_date", "2026-10-31", "2026-10-13", False),
        ("prices_include_tax", True, False, False),
        ("bill_to_name", None, None, True),
        ("bill_to_name", None, "x", False),
    ],
)
def test_values_match(path, expected, actual, match):
    assert values_match(path, expected, actual) is match


def test_score_field_outcomes():
    assert score_field("total", "10.00", "10.00", []).outcome == "correct"
    assert score_field("total", "10.00", "99.00", []).outcome == "wrong"
    assert score_field("total", "10.00", None, []).outcome == "missing"
    assert score_field("due_date", None, "2026-01-20", []).outcome == "hallucinated"


def test_alternatives_are_accepted_including_null():
    assert score_field("bill_to_name", None, "Consumidor final", ["Consumidor final"]).ok
    assert score_field("prices_include_tax", True, None, [None]).ok


def test_get_path():
    inv = {"vendor": {"name": "A"}, "line_items": [{"amount": "1"}, {"amount": "2"}]}
    assert get_path(inv, "vendor.name") == "A"
    assert get_path(inv, "line_items[1].amount") == "2"
    assert get_path(inv, "line_items[5].amount") is None


def _invoice(**changes):
    inv = json.loads(VALID_JSON)
    inv.update(changes)
    return inv


def test_score_fields_extra_and_missing_lines():
    expected = _invoice()
    two_lines = _invoice(line_items=expected["line_items"] * 2)
    results = {r.path: r for r in score_fields(expected, two_lines, {})}
    assert results["line_items.count"].outcome == "wrong"
    assert results["line_items[1].amount"].outcome == "hallucinated"  # extra line
    results = {r.path: r for r in score_fields(two_lines, expected, {})}
    assert results["line_items[1].amount"].outcome == "missing"  # dropped line


def test_tax_breakdown_rows_are_scored():
    rows = [{"rate": "23", "base": "12.89", "amount": "2.96"}]
    expected = _invoice(tax_breakdown=rows)
    misread = _invoice(tax_breakdown=[dict(rows[0], amount="2.56")])
    results = {r.path: r for r in score_fields(expected, misread, {})}
    assert results["tax_breakdown[0].amount"].outcome == "wrong"
    assert results["tax_breakdown[0].rate"].ok and results["tax_breakdown.count"].ok
    assert not results["tax_breakdown[0].amount"].critical  # `tax` is the critical one


def test_unlabeled_list_is_skipped():
    expected = _invoice()  # no tax_breakdown key: not labeled
    actual = _invoice(tax_breakdown=[{"rate": "20", "base": None, "amount": "2.00"}])
    paths = {r.path for r in score_fields(expected, actual, {})}
    assert not any(p.startswith("tax_breakdown") for p in paths)


def test_failed_extraction_scores_every_field_failed():
    results = score_fields(_invoice(), None, {})
    assert {r.outcome for r in results} == {"failed"}


def test_line_amount_is_critical_but_description_is_not():
    results = {r.path: r for r in score_fields(_invoice(), _invoice(), {})}
    assert results["line_items[0].amount"].critical
    assert not results["line_items[0].description"].critical


# --- flags and decisions --------------------------------------------------------


def test_score_flags_splits_unexpected_by_extraction_quality():
    expected = {("total_mismatch", "total")}
    predicted = {("total_mismatch", "total"), ("line_math", "line_items[0].amount")}
    good = score_flags(expected, predicted, extraction_wrong=False)
    assert good.matched == expected and good.false_alarms == {("line_math", "line_items[0].amount")}
    bad = score_flags(expected, predicted, extraction_wrong=True)
    assert bad.on_bad_extraction == {("line_math", "line_items[0].amount")} and not bad.false_alarms
    assert score_flags(expected, set(), False).missed == expected


def test_decisions():
    assert decide(should_review=True, flagged=False) == "unsafe_approval"
    assert decide(should_review=True, flagged=True) == "caught"
    assert decide(should_review=False, flagged=True) == "needless_review"
    assert decide(should_review=False, flagged=False) == "correct_approval"


# --- extract -> score plumbing ------------------------------------------------------


def test_extract_one_records_every_call(fake_llm, tmp_path):
    from app.config import get_settings
    from app.extraction.client import LLMError

    fake_llm.replies = [LLMError("503", transient=True), VALID_JSON]
    settings = get_settings().model_copy(
        update={"gemini_model": "gemini-3.5-flash-lite", "llm_retry_backoff_s": 0}
    )
    fx = load_fixtures({"syn-001"})[0]
    result = extract_one(fx, fake_llm, settings)
    assert result["extracted"]["invoice_number"] == "INV-1"
    first, second = result["attempts"]
    assert first["error"] == "503" and first["raw_response"] is None
    assert second["raw_response"] == VALID_JSON
    assert second["cost_usd"] == Decimal("0.000155")  # 100 in, 50 out at flash-lite rates


def test_memory_recorder_keeps_order():
    from app.extraction.core import Attempt

    rec = MemoryRecorder()
    a, b = Attempt("m", "v2"), Attempt("m", "v2")
    rec.record(a)
    rec.record(b)
    rec.update(a)
    assert rec.attempts == [a, b]


def _fake_run(tmp_path, answers):
    """A run directory as `extract` would write it, with chosen answers."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "manifest.json").write_text(
        json.dumps({"run_id": "t", "fixtures": list(answers)}), encoding="utf-8"
    )
    for fx_id, extracted in answers.items():
        attempt = {"model": "m", "raw_response": "{}", "latency_ms": 100, "cost_usd": "0.001",
                   "input_tokens": 10, "output_tokens": 5, "thinking_tokens": 0, "error": None}
        (tmp_path / "raw" / f"{fx_id}.json").write_text(
            json.dumps({"extracted": extracted, "attempts": [attempt]}), encoding="utf-8"
        )
    return tmp_path


def test_perfect_extraction_of_every_fixture_scores_perfectly(tmp_path):
    """If the model returned exactly the labels, the eval must report 100% accuracy,
    catch every planted error and duplicate, and never approve unsafely. The only
    false alarms allowed are the known check false positives recorded in the labels."""
    fixtures = load_fixtures()
    run = _fake_run(tmp_path, {fx.id: fx.expected for fx in fixtures})
    _, scores = score_run(run)
    report = build_report(scores)["all"]

    assert report["fields"]["accuracy_pct"] == 100.0
    assert report["flags"]["recall_pct"] == 100.0
    assert report["decisions"]["unsafe_approval"] == 0
    for s in scores:
        assert s.flags.false_alarms == s.known_check_fps, s.id
    dup = next(s for s in scores if s.id == "syn-025")
    assert (DUPLICATE_CODE, "invoice_number") in dup.flags.matched


def test_wrong_critical_field_without_a_flag_is_an_unsafe_approval(tmp_path):
    """A misread invoice number breaks no arithmetic, so no check fires and the invoice
    would be auto-approved: exactly the case the eval must surface."""
    fx = load_fixtures({"syn-001"})[0]
    wrong = dict(fx.expected)
    wrong["invoice_number"] = "NW-2026-3808"
    run = _fake_run(tmp_path, {"syn-001": wrong})
    _, [score] = score_run(run)
    assert score.decision == "unsafe_approval"
    assert build_report([score])["all"]["unsafe_approvals"] == ["syn-001"]


def test_failed_extraction_is_routed_to_review(tmp_path):
    run = _fake_run(tmp_path, {"syn-001": None})
    _, [score] = score_run(run)
    assert score.failed and score.decision == "caught"
