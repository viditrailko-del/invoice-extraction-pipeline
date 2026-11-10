"""Fields an invoice can't be paid or booked without."""

from app.checks.base import Flag
from app.schemas.extraction import ExtractedInvoice


def check_required_fields(inv: ExtractedInvoice) -> list[Flag]:
    required = {
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date,
        "vendor.name": inv.vendor.name,
        "currency": inv.currency,
        "total": inv.total,
    }
    return [
        Flag("missing_field", path, "Required field is missing", "error")
        for path, value in required.items()
        if value is None or (isinstance(value, str) and not value.strip())
    ]
