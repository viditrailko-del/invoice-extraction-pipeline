"""Tax breakdown checks. Modeled on pt-02, the real receipt that motivated them
(its 23% row and total are real; the 6% row's base is made up to fit)."""

from decimal import Decimal

from app.checks.tax import (
    check_tax_base_total,
    check_tax_breakdown_sum,
    check_tax_lines,
    check_tax_total_missing,
)
from app.schemas.extraction import ExtractedInvoice


def receipt(rows, tax="3.11", total="18.55") -> ExtractedInvoice:
    """pt-02: 23% row base 12.89 tax 2.96, 6% row base 2.55 tax 0.15. Total 18.55."""
    return ExtractedInvoice.model_validate(
        {"tax": tax, "total": total, "prices_include_tax": True, "tax_breakdown": rows}
    )


GOOD = [
    {"rate": "23", "base": "12.89", "amount": "2.96"},
    {"rate": "6", "base": "2.55", "amount": "0.15"},
]


def codes(flags):
    return [(f.code, f.field_path) for f in flags]


# --- per-row math ---------------------------------------------------------------


def test_row_math_ok():
    assert check_tax_lines(receipt(GOOD)) == []


def test_smudged_digit_is_caught():
    """The pt-02 misreading: 2,96 read as 2,56 (tax total read consistently as 2.71)."""
    rows = [dict(GOOD[0], amount="2.56"), GOOD[1]]
    flags = check_tax_lines(receipt(rows, tax="2.71"))
    assert codes(flags) == [("tax_line_math", "tax_breakdown[0].amount")]
    assert "12.89 x 23% = 2.96" in flags[0].reason


def test_row_math_one_cent_tolerance():
    # receipts often compute tax from the gross price, so base x rate can be a cent off
    rows = [dict(GOOD[0], amount="2.97")]
    assert check_tax_lines(receipt(rows)) == []


def test_row_without_base_is_skipped():
    # "VAT 20%: 275.59" has no printed base: nothing to verify, nothing invented
    assert check_tax_lines(receipt([{"rate": "20", "base": None, "amount": "275.59"}])) == []


def test_fractional_rate():
    rows = [{"rate": "8.75", "base": "1000.00", "amount": "87.50"}]
    assert check_tax_lines(receipt(rows)) == []


# --- rows vs tax total ------------------------------------------------------------


def test_rows_sum_to_tax_ok():
    assert check_tax_breakdown_sum(receipt(GOOD)) == []


def test_rows_not_summing_to_tax():
    flags = check_tax_breakdown_sum(receipt(GOOD, tax="2.71"))
    assert codes(flags) == [("tax_breakdown_mismatch", "tax")]


def test_sum_skipped_when_a_row_amount_missing():
    # pt-08: only the rate is printed, no tax amount
    rows = [{"rate": "13", "base": None, "amount": None}]
    assert check_tax_breakdown_sum(receipt(rows, tax=None)) == []
    assert check_tax_breakdown_sum(receipt(rows, tax="1.00")) == []


def test_sum_skipped_without_tax_total():
    assert check_tax_breakdown_sum(receipt(GOOD, tax=None)) == []


# --- tax total left empty -----------------------------------------------------------


def test_tax_total_present_ok():
    assert check_tax_total_missing(receipt(GOOD)) == []


def test_rows_without_tax_total_warns():
    """flash on pt-02: rows read correctly, tax total left empty."""
    flags = check_tax_total_missing(receipt(GOOD, tax=None))
    assert codes(flags) == [("tax_total_missing", "tax")]
    assert flags[0].severity == "warning"


def test_rate_only_rows_do_not_warn():
    # pt-08: only "13%" is printed, no amounts, no tax total
    rows = [{"rate": "13", "base": None, "amount": None}]
    assert check_tax_total_missing(receipt(rows, tax=None)) == []


def test_no_tax_at_all_does_not_warn():
    assert check_tax_total_missing(receipt([], tax=None)) == []


# --- bases + tax vs total -----------------------------------------------------------


def test_bases_plus_tax_equal_total():
    assert check_tax_base_total(receipt(GOOD)) == []


def test_misread_base_breaks_the_total():
    rows = [dict(GOOD[0], base="12.39"), GOOD[1]]  # 12.39 + 2.96 + 2.55 + 0.15 = 18.05
    flags = check_tax_base_total(receipt(rows))
    assert codes(flags) == [("tax_base_mismatch", "total")]


def test_tax_added_invoice_also_holds():
    # US style: subtotal 1000 + tax 87.50 = 1087.50
    inv = ExtractedInvoice.model_validate({
        "subtotal": "1000.00", "tax": "87.50", "total": "1087.50", "prices_include_tax": False,
        "tax_breakdown": [{"rate": "8.75", "base": "1000.00", "amount": "87.50"}],
    })
    assert check_tax_base_total(inv) == []


def test_base_total_skipped_when_any_base_missing():
    rows = [GOOD[0], dict(GOOD[1], base=None)]
    assert check_tax_base_total(receipt(rows)) == []


def test_no_breakdown_no_flags():
    inv = receipt([])
    assert check_tax_lines(inv) == check_tax_breakdown_sum(inv) == check_tax_base_total(inv) == []


def test_old_extractions_without_the_field_still_validate():
    # extracted_json stored before prompt v3 has no tax_breakdown key
    inv = ExtractedInvoice.model_validate({"tax": "1.00", "total": "10.00"})
    assert inv.tax_breakdown == []
    assert inv.tax == Decimal("1.00")
