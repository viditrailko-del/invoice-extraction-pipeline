"""Vendor tax ID check digits: catch a misread digit in the seller's tax number.

Why (eval run 20261111-053743-v3, receipt pt-02): the model read the vendor NIF
501419926 as 501415926. No arithmetic involves the tax ID, so nothing flagged it.
Many countries' tax IDs end in a check digit (EU VAT numbers, UK VAT, India GSTIN,
Norway org.nr, Canada BN...), so one misread digit almost always breaks it.

The per-country rules come from python-stdnum (100+ formats, maintained upstream)
rather than being hand-written here. This module only decides WHICH rules apply,
and only from evidence printed on the document, never from the currency:
  1. a country prefix on the ID itself ("DE287451900", "GB123456727"), else
  2. the vendor address: a country name, or a Portuguese NNNN-NNN postcode
     (Portuguese receipts rarely print the country).
No evidence, or two countries in the address -> skipped.

Only a failed CHECKSUM is flagged. A number that fits none of the country's formats
might be some other kind of ID (a company register number...), so it's skipped:
we flag what we can prove is wrong, not what we don't recognize. The US EIN has no
check digit, so US vendors are never flagged.
"""

import importlib
import re
import unicodedata
from types import ModuleType

from stdnum.exceptions import InvalidChecksum, ValidationError

from app.checks.base import Flag
from app.schemas.extraction import ExtractedInvoice

# Country -> stdnum modules for the tax IDs a vendor in that country prints.
_MODULES_BY_COUNTRY = {
    "pt": ["pt.nif"],
    "gb": ["gb.vat"],
    "ie": ["ie.vat"],
    "de": ["de.vat", "de.stnr"],
    "at": ["at.uid"],
    "fr": ["fr.tva", "fr.siren", "fr.siret"],
    "be": ["be.vat"],
    "nl": ["nl.btw"],
    "es": ["es.nif"],
    "it": ["it.iva"],
    "pl": ["pl.nip"],
    "no": ["no.mva", "no.orgnr"],
    "in": ["in_.gstin", "in_.pan"],
    "ca": ["ca.bn"],
    "us": [],  # EIN: no check digit to verify
}
_MODULES: dict[str, list[ModuleType]] = {
    cc: [importlib.import_module(f"stdnum.{name}") for name in names]
    for cc, names in _MODULES_BY_COUNTRY.items()
}

# An ID that starts with one of these is self-describing.
_PREFIXED = {
    **{cc: "eu.vat" for cc in ("at", "be", "bg", "cy", "cz", "de", "dk", "ee", "es", "fi", "fr",
                               "hr", "hu", "ie", "it", "lt", "lu", "lv", "mt", "nl", "pl", "pt",
                               "ro", "se", "si", "sk", "el")},
    "gb": "gb.vat",
    "no": "no.mva",
}

# Country names as printed in addresses (accents folded, lowercase), whole words.
_COUNTRY_WORDS = {
    "pt": ["portugal"],
    "gb": ["united kingdom", "uk", "england", "scotland", "wales"],
    "ie": ["ireland", "eire"],
    "de": ["deutschland", "germany"],
    "at": ["osterreich", "austria"],
    "fr": ["france"],
    "be": ["belgium", "belgique", "belgie"],
    "nl": ["nederland", "netherlands"],
    "es": ["espana", "spain"],
    "it": ["italia", "italy"],
    "pl": ["polska", "poland"],
    "no": ["norway", "norge"],
    "in": ["india"],
    "ca": ["canada"],
    "us": ["usa", "united states"],
}
_PT_POSTCODE = re.compile(r"\b\d{4}-\d{3}\b")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def address_country(address: str | None) -> str | None:
    """The one country the address names, or None if none / ambiguous."""
    if not address:
        return None
    text = _fold(address)
    found = {
        cc
        for cc, words in _COUNTRY_WORDS.items()
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)
    }
    if not found and _PT_POSTCODE.search(address):
        found = {"pt"}
    return found.pop() if len(found) == 1 else None


def _candidates(tax_id: str, address: str | None) -> list[ModuleType]:
    """Formats to try: the one a country prefix names, plus the address country's.
    Both, because letters can look like a prefix by accident: the Indian PAN
    ATXPM1234K starts with "AT" (Austria) but is valid as a PAN."""
    modules = []
    prefix = re.sub(r"[\s.\-]", "", tax_id)[:2].lower()
    if prefix in _PREFIXED:
        modules.append(importlib.import_module(f"stdnum.{_PREFIXED[prefix]}"))
    country = address_country(address)
    if country:
        modules += _MODULES.get(country, [])
    return modules


def check_vendor_tax_id(inv: ExtractedInvoice) -> list[Flag]:
    tax_id = inv.vendor.tax_id
    if not tax_id or not tax_id.strip():
        return []
    checksum_failed: list[ModuleType] = []
    for module in _candidates(tax_id, inv.vendor.address):
        try:
            module.validate(tax_id)
            return []  # valid under one of the country's formats
        except InvalidChecksum:
            checksum_failed.append(module)
        except ValidationError:
            pass  # not this format: might be another kind of ID
    if not checksum_failed:
        return []
    kind = (checksum_failed[0].__doc__ or checksum_failed[0].__name__).splitlines()[0].rstrip(".")
    return [
        Flag(
            "tax_id_invalid",
            "vendor.tax_id",
            f"{tax_id} fails the check digit of a {kind}: likely a misread digit",
            "error",
        )
    ]
