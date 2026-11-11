"""Portuguese NIF check digit. Real NIFs below come from the verified real fixtures."""

import pytest

from app.checks.tax_id import check_vendor_tax_id, nif_check_digit
from app.schemas.extraction import ExtractedInvoice

PT_ADDRESS = "Bairro Norton de Matos, 56 a 58, 3030-017 Coimbra"


def inv(tax_id, address=PT_ADDRESS) -> ExtractedInvoice:
    return ExtractedInvoice.model_validate({"vendor": {"name": "X", "address": address, "tax_id": tax_id}})


def codes(flags):
    return [(f.code, f.field_path) for f in flags]


@pytest.mark.parametrize(
    "nif", ["501419926", "503513709", "268504172", "112983600", "501810285", "506 668 720"]
)
def test_real_nifs_pass(nif):
    assert check_vendor_tax_id(inv(nif)) == []


def test_pt02_misread_digit_is_caught():
    """The eval's real failure: 501419926 read as 501415926."""
    flags = check_vendor_tax_id(inv("501415926"))
    assert codes(flags) == [("tax_id_invalid", "vendor.tax_id")]
    assert "should be 0" in flags[0].reason


def test_check_digit_rule():
    assert nif_check_digit("50141992") == 6  # 148 % 11 = 5 -> 11 - 5
    assert nif_check_digit("50141592") == 0  # remainder 0 -> 0 (and remainder 1 -> 0)


def test_pt_prefix_is_enough_evidence():
    assert check_vendor_tax_id(inv("PT501419926", address=None)) == []
    assert codes(check_vendor_tax_id(inv("PT501415926", address=None))) == [
        ("tax_id_invalid", "vendor.tax_id")
    ]


def test_pt_prefix_but_wrong_length():
    assert codes(check_vendor_tax_id(inv("PT50141992", address=None))) == [
        ("tax_id_invalid", "vendor.tax_id")
    ]


def test_portugal_in_address_is_evidence():
    assert check_vendor_tax_id(inv("501415926", address="Lisboa (Portugal)"))


@pytest.mark.parametrize(
    ("tax_id", "address"),
    [
        ("123 4567 89", "12 Harbour Street, Bristol BS1 4XY, United Kingdom"),  # UK VAT, no GB
        ("732829320", "8 Rue des Lilas, 69003 Lyon, France"),  # a French SIREN
        ("501415926", None),  # 9 digits, but nothing says Portugal
        ("94-3812775", "San Francisco, CA 94105, USA"),
    ],
)
def test_other_countries_are_never_checked(tax_id, address):
    assert check_vendor_tax_id(inv(tax_id, address)) == []


def test_pt_address_with_a_non_nif_id_is_skipped():
    assert check_vendor_tax_id(inv("ABC-123")) == []


def test_missing_tax_id_is_not_this_checks_job():
    assert check_vendor_tax_id(inv(None)) == []
