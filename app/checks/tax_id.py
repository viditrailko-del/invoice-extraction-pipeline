"""Vendor tax ID check digits: catch a misread digit in the seller's tax number.

Why (eval run 20261111-053743-v3, receipt pt-02): the model read the vendor NIF
501419926 as 501415926. No arithmetic involves the tax ID, so nothing flagged it,
and a wrong seller ID would have been auto-approved. Portuguese NIFs end in a
mod-11 check digit, so a single misread digit almost always breaks it.

When does it apply? Only with evidence ON THE DOCUMENT that the vendor is
Portuguese: a "PT" prefix on the ID, or a Portuguese postcode (NNNN-NNN) or
"Portugal" in the vendor address. Never from the currency: many countries use
9-digit IDs (a UK VAT number printed without "GB", a French SIREN), and they would
fail this checksum for no reason.
"""

import re

from app.checks.base import Flag
from app.schemas.extraction import ExtractedInvoice

PT_POSTCODE = re.compile(r"\b\d{4}-\d{3}\b")
_WEIGHTS = (9, 8, 7, 6, 5, 4, 3, 2)


def nif_check_digit(first8: str) -> int:
    """The 9th digit a valid NIF must have, given its first 8 digits."""
    remainder = sum(int(d) * w for d, w in zip(first8, _WEIGHTS, strict=True)) % 11
    return 0 if remainder < 2 else 11 - remainder


def _is_portuguese(inv: ExtractedInvoice, compact_id: str) -> bool:
    if compact_id.startswith("PT"):
        return True
    address = inv.vendor.address or ""
    return bool(PT_POSTCODE.search(address)) or "portugal" in address.casefold()


def check_vendor_tax_id(inv: ExtractedInvoice) -> list[Flag]:
    tax_id = inv.vendor.tax_id
    if not tax_id:
        return []
    compact = re.sub(r"\s", "", tax_id).upper()  # "506 668 720" is printed with spaces
    if not _is_portuguese(inv, compact):
        return []
    digits = compact.removeprefix("PT")
    if not re.fullmatch(r"\d{9}", digits):
        if compact.startswith("PT"):  # says it's a PT number but isn't 9 digits
            return [Flag("tax_id_invalid", "vendor.tax_id",
                         f"{tax_id} is marked PT but is not a 9-digit NIF", "error")]
        return []  # PT address, but some other kind of ID: nothing we can verify
    expected = nif_check_digit(digits[:8])
    if int(digits[8]) == expected:
        return []
    return [
        Flag(
            "tax_id_invalid",
            "vendor.tax_id",
            f"Portuguese NIF {tax_id} fails its check digit (last digit should be {expected})",
            "error",
        )
    ]
