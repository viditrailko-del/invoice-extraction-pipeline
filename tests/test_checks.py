"""Unit tests for the pure deterministic checks (Phase 3).

No DB, no client: each check takes an ExtractedInvoice and returns flags.
Every check has a passing case, a failing case, and edge cases.
"""

from datetime import date

from app.checks.arithmetic import (
    check_line_items,
    check_negative_amounts,
    check_subtotal,
    check_total,
)
from app.checks.dates import check_due_date_order, check_invoice_date_plausible
from app.checks.required import check_required_fields
from app.checks.runner import run_checks
from app.schemas.extraction import ExtractedInvoice

TODAY = date(2026, 11, 10)


def inv(**overrides) -> ExtractedInvoice:
    """A fully consistent invoice; override fields to break it."""
    data = {
        "invoice_number": "INV-1",
        "invoice_date": "2026-11-01",
        "due_date": "2026-12-01",
        "vendor": {"name": "Acme"},
        "currency": "USD",
        "line_items": [
            {"description": "A", "quantity": "2", "unit_price": "10.00", "amount": "20.00"},
            {"description": "B", "quantity": "3", "unit_price": "5.50", "amount": "16.50"},
        ],
        "subtotal": "36.50",
        "tax": "3.65",
        "discount": None,
        "total": "40.15",
        "prices_include_tax": False,
    }
    data.update(overrides)
    return ExtractedInvoice.model_validate(data)


def paths(flags) -> list[str]:
    return [f.field_path for f in flags]


# --- whole runner -------------------------------------------------------------


def test_consistent_invoice_has_no_flags():
    assert run_checks(inv(), TODAY) == []


# --- required fields ------------------------------------------------------------


def test_required_fields_present():
    assert check_required_fields(inv()) == []


def test_missing_required_fields_are_errors():
    flags = check_required_fields(inv(invoice_number=None, total=None, vendor={}))
    assert paths(flags) == ["invoice_number", "vendor.name", "total"]
    assert {f.severity for f in flags} == {"error"}
    assert {f.code for f in flags} == {"missing_field"}


def test_blank_string_counts_as_missing():
    assert paths(check_required_fields(inv(invoice_number="   "))) == ["invoice_number"]


# --- line items ---------------------------------------------------------------


def test_line_math_ok():
    assert check_line_items(inv()) == []


def test_line_math_wrong():
    lines = [{"quantity": "2", "unit_price": "10.00", "amount": "25.00"}]
    [flag] = check_line_items(inv(line_items=lines))
    assert flag.field_path == "line_items[0].amount"
    assert flag.severity == "error"
    assert "20.00" in flag.reason and "25.00" in flag.reason


def test_line_math_rounds_half_up_to_cents():
    # 3 x 0.125 = 0.375 -> printed as 0.38 (half up), not flagged
    lines = [{"quantity": "3", "unit_price": "0.125", "amount": "0.38"}]
    assert check_line_items(inv(line_items=lines)) == []


def test_line_math_within_one_cent_tolerance():
    lines = [{"quantity": "1", "unit_price": "10.00", "amount": "10.01"}]
    assert check_line_items(inv(line_items=lines)) == []


def test_line_with_missing_number_is_skipped():
    lines = [{"quantity": None, "unit_price": "10.00", "amount": "99.00"}]
    assert check_line_items(inv(line_items=lines)) == []


def test_line_index_in_path_points_at_bad_line():
    lines = [
        {"quantity": "1", "unit_price": "1.00", "amount": "1.00"},
        {"quantity": "1", "unit_price": "1.00", "amount": "1.00"},
        {"quantity": "2", "unit_price": "1.00", "amount": "1.00"},
    ]
    assert paths(check_line_items(inv(line_items=lines))) == ["line_items[2].amount"]


# --- subtotal -----------------------------------------------------------------


def test_subtotal_matches_lines():
    assert check_subtotal(inv()) == []


def test_subtotal_mismatch():
    [flag] = check_subtotal(inv(subtotal="40.00"))
    assert flag.field_path == "subtotal"
    assert "36.50" in flag.reason


def test_subtotal_skipped_when_a_line_amount_missing():
    lines = [{"amount": "20.00"}, {"amount": None}]
    assert check_subtotal(inv(line_items=lines, subtotal="999.00")) == []


def test_subtotal_skipped_without_line_items():
    assert check_subtotal(inv(line_items=[])) == []


# --- total --------------------------------------------------------------------


def test_total_ok():
    assert check_total(inv()) == []


def test_total_mismatch():
    [flag] = check_total(inv(total="50.00"))
    assert flag.field_path == "total"
    assert flag.severity == "error"


def test_total_with_discount_before_subtotal():
    # subtotal is pre-discount: 36.50 - 6.50 + 3.00 = 33.00
    assert check_total(inv(discount="6.50", tax="3.00", total="33.00")) == []


def test_total_with_discount_already_in_subtotal():
    # subtotal already post-discount: 36.50 + 3.65 = 40.15, discount just informational
    assert check_total(inv(discount="6.50")) == []


def test_total_with_negative_printed_discount():
    assert check_total(inv(discount="-6.50", tax="3.00", total="33.00")) == []


def test_total_without_tax_or_subtotal_uses_line_sum():
    assert check_total(inv(subtotal=None, tax=None, total="36.50")) == []
    assert paths(check_total(inv(subtotal=None, tax=None, total="30.00"))) == ["total"]


def receipt(**overrides) -> ExtractedInvoice:
    """Real Portuguese receipt (Zenodo 7213544): 10,00 + 2,20 = Total 12,20, IVA 2,28 included."""
    data = {
        "line_items": [{"amount": "10.00"}, {"amount": "2.20"}],
        "subtotal": None,
        "tax": "2.28",
        "total": "12.20",
        "prices_include_tax": True,
    }
    data.update(overrides)
    return ExtractedInvoice.model_validate(data)


def test_tax_included_receipt_ok():
    assert check_total(receipt()) == []


def test_tax_included_receipt_flagged_under_tax_added_rule():
    # the false alarm we'd raise if we assumed "tax added" everywhere
    assert paths(check_total(receipt(prices_include_tax=False))) == ["total"]


def test_tax_included_but_total_has_tax_added_again():
    [flag] = check_total(receipt(total="14.48"))  # 12.20 + 2.28
    assert "tax included" in flag.reason


def test_tax_included_with_discount():
    assert check_total(receipt(discount="1.20", total="11.00")) == []


def test_tax_added_catches_subtotal_read_as_total():
    # classic LLM slip on a tax-added invoice: picks the subtotal line as the total
    assert paths(check_total(inv(total="36.50"))) == ["total"]


def test_unknown_convention_accepts_either_reading():
    assert check_total(inv(prices_include_tax=None)) == []  # tax added
    assert check_total(receipt(prices_include_tax=None)) == []  # tax included
    assert paths(check_total(inv(prices_include_tax=None, total="50.00"))) == ["total"]


def test_total_skipped_when_nothing_to_compare():
    assert check_total(inv(subtotal=None, line_items=[], total="12.00")) == []


# --- negative amounts ---------------------------------------------------------


def test_positive_amounts_ok():
    assert check_negative_amounts(inv()) == []


def test_negative_amounts_are_warnings():
    lines = [{"quantity": "-1", "unit_price": "10.00", "amount": "-10.00"}]
    flags = check_negative_amounts(inv(line_items=lines, total="-10.00"))
    assert paths(flags) == ["total", "line_items[0].quantity", "line_items[0].amount"]
    assert {f.severity for f in flags} == {"warning"}


def test_negative_discount_not_flagged():
    assert check_negative_amounts(inv(discount="-5.00")) == []


# --- dates --------------------------------------------------------------------


def test_due_date_after_invoice_date_ok():
    assert check_due_date_order(inv()) == []


def test_due_date_same_day_ok():
    assert check_due_date_order(inv(due_date="2026-11-01")) == []


def test_due_date_before_invoice_date():
    [flag] = check_due_date_order(inv(due_date="2026-10-01"))
    assert flag.field_path == "due_date"
    assert flag.severity == "error"


def test_due_date_missing_skipped():
    assert check_due_date_order(inv(due_date=None)) == []


def test_invoice_date_today_ok():
    assert check_invoice_date_plausible(inv(invoice_date="2026-11-10"), TODAY) == []


def test_invoice_date_in_future_warns():
    [flag] = check_invoice_date_plausible(inv(invoice_date="2026-11-11"), TODAY)
    assert flag.severity == "warning"
    assert "future" in flag.reason


def test_invoice_date_too_old_warns():
    [flag] = check_invoice_date_plausible(inv(invoice_date="2024-01-01"), TODAY)
    assert "2 years" in flag.reason


def test_invoice_date_exactly_at_age_limit_ok():
    assert check_invoice_date_plausible(inv(invoice_date="2024-11-10"), TODAY) == []
