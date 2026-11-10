"""Date sanity checks.

`today` is a parameter, not date.today() inside the function: that keeps the check
pure and lets tests pin the date instead of breaking next year.
"""

from datetime import date, timedelta

from app.checks.base import Flag
from app.schemas.extraction import ExtractedInvoice

MAX_INVOICE_AGE = timedelta(days=730)  # ~2 years: older usually means a misread year


def check_due_date_order(inv: ExtractedInvoice) -> list[Flag]:
    if inv.invoice_date is None or inv.due_date is None:
        return []
    if inv.due_date >= inv.invoice_date:
        return []
    return [
        Flag(
            "due_before_invoice",
            "due_date",
            f"Due date {inv.due_date} is before invoice date {inv.invoice_date}",
            "error",
        )
    ]


def check_invoice_date_plausible(inv: ExtractedInvoice, today: date) -> list[Flag]:
    d = inv.invoice_date
    if d is None:
        return []
    if d > today:
        return [Flag("date_implausible", "invoice_date", f"Invoice date {d} is in the future", "warning")]
    if today - d > MAX_INVOICE_AGE:
        return [
            Flag("date_implausible", "invoice_date", f"Invoice date {d} is more than 2 years old", "warning")
        ]
    return []
