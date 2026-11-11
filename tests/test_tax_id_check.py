"""Vendor tax ID check digits (rules from python-stdnum; we test which rules apply).

Real Portuguese NIFs come from the verified real fixtures; the other valid IDs are
the synthetic vendors' (made valid with stdnum)."""

import pytest

from app.checks.tax_id import address_country, check_vendor_tax_id
from app.schemas.extraction import ExtractedInvoice

PT_ADDRESS = "Bairro Norton de Matos, 56 a 58, 3030-017 Coimbra"


def inv(tax_id, address=PT_ADDRESS) -> ExtractedInvoice:
    return ExtractedInvoice.model_validate(
        {"vendor": {"name": "X", "address": address, "tax_id": tax_id}}
    )


def flagged(tax_id, address=PT_ADDRESS) -> bool:
    flags = check_vendor_tax_id(inv(tax_id, address))
    assert all((f.code, f.field_path) == ("tax_id_invalid", "vendor.tax_id") for f in flags)
    return bool(flags)


# --- Portugal (the real eval data) -------------------------------------------------


@pytest.mark.parametrize(
    "nif", ["501419926", "503513709", "268504172", "112983600", "501810285", "506 668 720"]
)
def test_real_nifs_pass(nif):
    assert not flagged(nif)


def test_pt02_misread_digit_is_caught():
    """The eval's real failure: 501419926 read as 501415926."""
    flags = check_vendor_tax_id(inv("501415926"))
    assert len(flags) == 1 and "501415926" in flags[0].reason


# --- other countries: valid passes, one changed digit is caught ----------------------


@pytest.mark.parametrize(
    ("valid", "misread", "address"),
    [
        ("GB123456727", "GB123456721", None),  # prefix is enough evidence
        ("DE287451900", "DE287451909", None),
        ("PT509876544", "PT509876543", None),
        ("NO987654325MVA", "NO987654321MVA", None),
        ("FR40303265045", "FR40303265046", None),
        ("27AABCS1429B1ZU", "27AABCS1429B1Z5", "Plot 22, Pune 411057, Maharashtra, India"),
        ("123456782RT0001", "123456789RT0001", "220 King St W, Toronto, ON M5H 1K4, Canada"),
        ("123 4567 27", "123 4567 21", "12 Harbour Street, Bristol BS1 4XY, United Kingdom"),
        ("732829320", "732829321", "8 Rue des Lilas, 69003 Lyon, France"),  # SIREN
    ],
)
def test_misread_digit_caught_in_each_country(valid, misread, address):
    assert not flagged(valid, address)
    assert flagged(misread, address)


# --- when NOT to check ----------------------------------------------------------------


def test_letters_that_look_like_a_prefix():
    # Indian PAN starting "AT" (Austria's VAT prefix): the address says India, PAN is valid
    assert not flagged("ATXPM1234K", "Plot 22, Pune 411057, Maharashtra, India")


def test_us_ein_has_no_check_digit():
    assert not flagged("94-3812775", "480 Mission Street, San Francisco, CA 94105, USA")
    assert not flagged("94-3812779", "480 Mission Street, San Francisco, CA 94105, USA")


def test_no_evidence_no_check():
    # 9 digits that fail the PT rule, but nothing on the document says Portugal
    assert not flagged("501415926", address=None)
    assert not flagged("501415926", address="Somewhere 12, Atlantis")


def test_unrecognized_format_is_skipped_not_flagged():
    # a UK company registration number is not a VAT number: we can't verify it
    assert not flagged("09876543", "1 High St, London, United Kingdom")
    assert not flagged("ABC-123")


def test_missing_tax_id_is_not_this_checks_job():
    assert not flagged(None)
    assert not flagged("  ")


# --- country from address ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "country"),
    [
        ("Industriestraße 14, 70565 Stuttgart, Deutschland", "de"),
        ("Avenida X, 1069-413 Lisboa (Portugal)", "pt"),
        ("RUA JOAO DE RUAO N-27, 3000-229 COIMBRA", "pt"),  # PT postcode, no country name
        ("Karl Johans gate 5, 0154 Oslo, Norway", "no"),
        ("Rue X, Paris, France; Branch office: Madrid, Spain", None),  # two countries
        ("Rua do Brasil, 306-308", None),  # no evidence
        (None, None),
    ],
)
def test_address_country(address, country):
    assert address_country(address) == country
