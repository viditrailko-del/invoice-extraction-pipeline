"""Field-path helpers used by corrections (pure, no DB)."""

import pytest

from app.review.paths import InvalidPath, get_at, parse_path, set_at


def data():
    return {"total": "1.00", "vendor": {"name": "Acme"}, "line_items": [{"amount": "1"}, {"amount": "2"}]}


def test_parse_path():
    assert parse_path("total") == ["total"]
    assert parse_path("vendor.name") == ["vendor", "name"]
    assert parse_path("line_items[1].amount") == ["line_items", 1, "amount"]


@pytest.mark.parametrize("bad", ["", "a..b", "items[x]", "items[-1]", "1abc", "a.b["])
def test_parse_rejects_malformed(bad):
    with pytest.raises(InvalidPath):
        parse_path(bad)


def test_get_and_set():
    d = data()
    set_at(d, "line_items[1].amount", "9")
    set_at(d, "vendor.name", "Beta")
    assert get_at(d, "line_items[1].amount") == "9"
    assert d["vendor"]["name"] == "Beta"


def test_set_whole_list():
    d = data()
    set_at(d, "line_items", [])
    assert d["line_items"] == []


@pytest.mark.parametrize("path", ["nope", "vendor.nope", "line_items[5].amount", "total.x"])
def test_set_never_creates_fields(path):
    with pytest.raises(InvalidPath):
        set_at(data(), path, "x")
