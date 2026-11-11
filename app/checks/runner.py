"""Run every deterministic check on one extraction.

Phase 4 will call this again after a human correction, so it must stay pure.
(Duplicate detection needs the DB, so it lives in extraction/service.py instead.)
"""

from datetime import date

from app.checks.arithmetic import (
    check_line_items,
    check_negative_amounts,
    check_subtotal,
    check_total,
)
from app.checks.base import Flag
from app.checks.dates import check_due_date_order, check_invoice_date_plausible
from app.checks.required import check_required_fields
from app.checks.tax import (
    check_tax_base_total,
    check_tax_breakdown_sum,
    check_tax_lines,
    check_tax_total_missing,
)
from app.checks.tax_id import check_vendor_tax_id
from app.schemas.extraction import ExtractedInvoice


def run_checks(inv: ExtractedInvoice, today: date) -> list[Flag]:
    return [
        *check_required_fields(inv),
        *check_line_items(inv),
        *check_subtotal(inv),
        *check_total(inv),
        *check_tax_lines(inv),
        *check_tax_breakdown_sum(inv),
        *check_tax_total_missing(inv),
        *check_tax_base_total(inv),
        *check_vendor_tax_id(inv),
        *check_negative_amounts(inv),
        *check_due_date_order(inv),
        *check_invoice_date_plausible(inv, today),
    ]
