"""The response schema sent to Gemini (generated from ExtractedInvoice)."""

from decimal import Decimal

from app.extraction.core import response_schema
from app.schemas.extraction import ExtractedInvoice

NUMERIC = {
    ("ExtractedInvoice", "subtotal"), ("ExtractedInvoice", "tax"),
    ("ExtractedInvoice", "discount"), ("ExtractedInvoice", "total"),
    ("LineItem", "quantity"), ("LineItem", "unit_price"), ("LineItem", "amount"),
    ("TaxLine", "rate"), ("TaxLine", "base"), ("TaxLine", "amount"),
}


def _properties():
    schema = response_schema()
    yield from (("ExtractedInvoice", k, v) for k, v in schema["properties"].items())
    for model, sub in schema["$defs"].items():
        yield from ((model, k, v) for k, v in sub.get("properties", {}).items())


def _types(prop) -> set[str]:
    return {t["type"] for t in prop.get("anyOf", [prop]) if "type" in t}


def test_numbers_are_json_numbers_only():
    """No string branch: Gemini can't answer "2,79" or "1 C" (eval 20261111-055744)."""
    seen = set()
    for model, name, prop in _properties():
        if (model, name) in NUMERIC:
            seen.add((model, name))
            assert _types(prop) == {"number", "null"}, f"{model}.{name}: {prop}"
    assert seen == NUMERIC  # every numeric field was found (a rename would fail here)


def test_no_field_anywhere_allows_free_strings_for_numbers():
    # Any field that accepts numbers must not also accept arbitrary strings.
    for model, name, prop in _properties():
        types = _types(prop)
        assert not ({"number", "string"} <= types), f"{model}.{name} allows number OR string"


def test_validation_still_accepts_numeric_strings():
    """Stored extractions and fixtures write money as strings ("22.00")."""
    inv = ExtractedInvoice.model_validate({"total": "22.00", "line_items": [{"quantity": "9.5"}]})
    assert inv.total == Decimal("22.00") and inv.line_items[0].quantity == Decimal("9.5")


def test_json_numbers_parse_exactly():
    inv = ExtractedInvoice.model_validate_json('{"total": 0.1, "tax": 1858.8}')
    assert inv.total == Decimal("0.1") and inv.tax == Decimal("1858.8")


def test_descriptions_survive():
    total = response_schema()["properties"]["total"]
    assert total["description"] == "Final amount due"
