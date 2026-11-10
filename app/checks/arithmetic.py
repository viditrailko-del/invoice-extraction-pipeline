"""Never trust LLM math (CLAUDE.md rule 1): recompute every total in code.

Each check only runs when the numbers it needs are present. A missing number is
the required-fields check's job; here we only flag numbers that disagree.
"""

from decimal import Decimal

from app.checks.base import Flag, money_equal, to_cents
from app.schemas.extraction import ExtractedInvoice


def check_line_items(inv: ExtractedInvoice) -> list[Flag]:
    """quantity x unit_price must equal the printed line amount."""
    flags = []
    for i, line in enumerate(inv.line_items):
        if line.quantity is None or line.unit_price is None or line.amount is None:
            continue
        expected = to_cents(line.quantity * line.unit_price)
        if not money_equal(expected, line.amount):
            flags.append(
                Flag(
                    "line_math",
                    f"line_items[{i}].amount",
                    f"{line.quantity} x {line.unit_price} = {expected}, but amount is {line.amount}",
                    "error",
                )
            )
    return flags


def _lines_sum(inv: ExtractedInvoice) -> Decimal | None:
    """Sum of line amounts, or None if there are no lines or any amount is missing
    (a partial sum would produce false mismatches)."""
    amounts = [line.amount for line in inv.line_items]
    if not amounts or any(a is None for a in amounts):
        return None
    return sum(amounts, Decimal("0"))


def check_subtotal(inv: ExtractedInvoice) -> list[Flag]:
    """Line amounts must add up to the subtotal."""
    lines_sum = _lines_sum(inv)
    if inv.subtotal is None or lines_sum is None:
        return []
    if money_equal(lines_sum, inv.subtotal):
        return []
    return [
        Flag(
            "subtotal_mismatch",
            "subtotal",
            f"Line items sum to {lines_sum}, but subtotal is {inv.subtotal}",
            "error",
        )
    ]


def check_total(inv: ExtractedInvoice) -> list[Flag]:
    """The total must follow from subtotal, discount and tax.

    Whether tax is ADDED (subtotal + tax = total, e.g. US, B2B invoices) or already
    INCLUDED (lines add up to the total, e.g. EU shop receipts) depends on the
    document, so it's an extracted field, not a guess:
      prices_include_tax = False -> total = base - discount + tax
      prices_include_tax = True  -> total = base - discount   (tax is "of which")
      prices_include_tax = None  -> accept either reading
    Being strict when the document says which applies catches the classic LLM
    mistake of reading the subtotal line as the total on a tax-added invoice.

    Also tolerated: the subtotal printed before or after the discount, and a
    discount printed as 50.00 or -50.00.
    """
    if inv.total is None:
        return []
    base = inv.subtotal if inv.subtotal is not None else _lines_sum(inv)
    if base is None:
        return []
    tax = inv.tax or Decimal("0")
    discount = abs(inv.discount or Decimal("0"))
    # first of each pair: discount not yet applied; second: already in the subtotal
    added = [base + tax - discount, base + tax]
    included = [base - discount, base]
    readings = {False: added, True: included, None: added + included}[inv.prices_include_tax]
    if any(money_equal(expected, inv.total) for expected in readings):
        return []
    base_name = "subtotal" if inv.subtotal is not None else "line items"
    convention = {False: "tax added", True: "tax included", None: "tax added or included"}
    return [
        Flag(
            "total_mismatch",
            "total",
            f"{base_name} {base}, discount {discount}, tax {tax} "
            f"({convention[inv.prices_include_tax]}) gives {readings[0]}, but total is {inv.total}",
            "error",
        )
    ]


def check_negative_amounts(inv: ExtractedInvoice) -> list[Flag]:
    """Negative money is unusual (credit notes exist, so it's a warning, not an error).

    Discount is excluded: printing it as a negative number is normal.
    """
    fields: dict[str, Decimal | None] = {
        "subtotal": inv.subtotal,
        "tax": inv.tax,
        "total": inv.total,
    }
    for i, line in enumerate(inv.line_items):
        fields[f"line_items[{i}].quantity"] = line.quantity
        fields[f"line_items[{i}].unit_price"] = line.unit_price
        fields[f"line_items[{i}].amount"] = line.amount
    return [
        Flag("negative_amount", path, f"Negative value {value} (credit note?)", "warning")
        for path, value in fields.items()
        if value is not None and value < 0
    ]
