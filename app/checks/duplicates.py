"""Business-duplicate detection: is this the same INVOICE (not the same file)?

The same paper invoice scanned twice produces different bytes (different SHA-256),
so file hashing can't catch it. Instead we compare what the invoice SAYS:
same vendor + same invoice number = almost certainly the same invoice.

Pure functions only: no DB, no network (CLAUDE.md rule 3).
"""

import re

from app.schemas.extraction import ExtractedInvoice

DUPLICATE_CODE = "duplicate"  # field_flags.code for business-duplicate flags

# Legal-form words that vary between documents from the same company.
_COMPANY_SUFFIXES = re.compile(
    r"\b(ltd|limited|llc|inc|incorporated|corp|corporation|co|company|plc|gmbh|ag|sa|bv"
    r"|pvt|private|pte|llp|lp|srl|spa)\b"
)


def normalize_text(value: str) -> str:
    """'NW-2026-0417' and 'nw 2026 0417' -> 'nw20260417'."""
    return re.sub(r"[^0-9a-z]", "", value.casefold())


def normalize_vendor(name: str) -> str:
    """'Northwind Cloud Services Ltd.' and 'NORTHWIND CLOUD SERVICES LIMITED' match."""
    words = re.sub(r"[^0-9a-z ]", " ", name.casefold())
    return normalize_text(_COMPANY_SUFFIXES.sub(" ", words))


def make_dup_key(inv: ExtractedInvoice) -> str | None:
    """'<vendor>|<invoice number>', or None if we can't identify the invoice.

    Without both a vendor and an invoice number we can't claim two invoices are
    the same, so no key (better to miss a duplicate than to flag false ones).
    """
    if not inv.invoice_number:
        return None
    number = normalize_text(inv.invoice_number)
    vendor = normalize_vendor(inv.vendor.name) if inv.vendor.name else ""
    if not vendor and inv.vendor.tax_id:
        vendor = "tax:" + normalize_text(inv.vendor.tax_id)
    if not number or not vendor:
        return None
    return f"{vendor}|{number}"[:300]
