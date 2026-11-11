"""Tax breakdown checks: each printed tax row must add up, and the rows must add up.

Why this exists (eval run 20261111, receipt pt-02): a smudged digit made the model
read the 23% tax as 2,56 instead of 2,96. On a tax-included receipt the total check
never looks at the tax amount, so the wrong value would have been auto-approved.
The row's own arithmetic exposes it: 12,89 x 23% = 2,96, not 2,56.

Each check runs only on rows/fields that are present; a missing number is never
filled in by computing it (rule 1: we verify LLM numbers, we don't invent them).
"""

from decimal import Decimal

from app.checks.base import Flag, money_equal, to_cents
from app.schemas.extraction import ExtractedInvoice

HUNDRED = Decimal("100")


def check_tax_lines(inv: ExtractedInvoice) -> list[Flag]:
    """base x rate must equal the printed tax amount on each row."""
    flags = []
    for i, row in enumerate(inv.tax_breakdown):
        if row.rate is None or row.base is None or row.amount is None:
            continue
        expected = to_cents(row.base * row.rate / HUNDRED)
        if not money_equal(expected, row.amount):
            flags.append(
                Flag(
                    "tax_line_math",
                    f"tax_breakdown[{i}].amount",
                    f"{row.base} x {row.rate}% = {expected}, but tax amount is {row.amount}",
                    "error",
                )
            )
    return flags


def check_tax_breakdown_sum(inv: ExtractedInvoice) -> list[Flag]:
    """The rows' tax amounts must add up to the tax total."""
    amounts = [row.amount for row in inv.tax_breakdown]
    if inv.tax is None or not amounts or any(a is None for a in amounts):
        return []
    rows_sum = sum(amounts, Decimal("0"))
    if money_equal(rows_sum, inv.tax):
        return []
    return [
        Flag(
            "tax_breakdown_mismatch",
            "tax",
            f"Tax rows sum to {rows_sum}, but tax is {inv.tax}",
            "error",
        )
    ]


def check_tax_total_missing(inv: ExtractedInvoice) -> list[Flag]:
    """Tax rows with amounts but no tax total: the total may have been skipped.

    Found by eval run 20261111-113016-v3-merged: gemini-3.5-flash read pt-02's tax
    rows correctly but left `tax` empty, and no other check looked at an empty tax.
    A warning, not an error: some receipts print only per-rate tax (pt-07), and then
    an empty total is the right answer. We still never compute it (rule 1).
    """
    if inv.tax is not None:
        return []
    if not any(row.amount is not None for row in inv.tax_breakdown):
        return []
    return [
        Flag(
            "tax_total_missing",
            "tax",
            "Tax rows have amounts, but the tax total is empty",
            "warning",
        )
    ]


def check_tax_base_total(inv: ExtractedInvoice) -> list[Flag]:
    """Taxable bases plus their tax must add up to the total.

    True whether tax is added or included: the total is always net + tax (the
    bases are already after any discount). Needs every row's base and amount.
    """
    rows = inv.tax_breakdown
    if inv.total is None or not rows:
        return []
    if any(r.base is None or r.amount is None for r in rows):
        return []
    gross = sum((r.base + r.amount for r in rows), Decimal("0"))  # type: ignore[operator]
    if money_equal(gross, inv.total):
        return []
    return [
        Flag(
            "tax_base_mismatch",
            "total",
            f"Tax bases + tax add up to {gross}, but total is {inv.total}",
            "error",
        )
    ]
