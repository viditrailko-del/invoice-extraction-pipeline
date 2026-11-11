"""Guards the eval fixtures (answer keys) against drift.

If someone changes the schema or a check, these tests fail until the fixtures are
regenerated (python -m scripts.make_fixtures) or the change is reconsidered.

Two kinds of fixture:
  synthetic  generated; expected_flags == exactly what run_checks raises (planted errors)
  real       hand-labeled; the documents are genuine and correct, so expected_flags == [].
             If the checks still raise something on the TRUE data, that's a known check
             weakness, recorded in check_false_positives (and asserted, so it's visible).
Real images aren't in git (scripts/fetch_real_fixtures.py); their existence is only
checked when they've been fetched.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.checks.runner import run_checks
from app.review.paths import InvalidPath, get_at
from app.schemas.extraction import ExtractedInvoice

FIXTURES = Path(__file__).parents[1] / "eval" / "fixtures"
FILES = sorted(FIXTURES.rglob("*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def is_real(fx: dict) -> bool:
    return isinstance(fx["source"], dict)


def test_fixtures_exist():
    fixtures = [load(p) for p in FILES]
    assert sum(not is_real(f) for f in fixtures) >= 20
    assert sum(is_real(f) for f in fixtures) >= 10


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_fixture_is_valid(path):
    fx = load(path)
    doc = path.parent / fx["file"]
    if not is_real(fx):
        assert doc.is_file(), "document file missing"
    elif not doc.is_file():
        pass  # real images are fetched on demand, not committed

    # answer key must satisfy the same schema the LLM must satisfy (and round-trip)
    inv = ExtractedInvoice.model_validate(fx["expected"])
    assert inv.model_dump(mode="json") == fx["expected"]

    raised = [{"code": f.code, "field_path": f.field_path}
              for f in run_checks(inv, date.fromisoformat(fx["as_of"]))]
    if is_real(fx):
        assert fx["expected_flags"] == []  # genuine documents: nothing is actually wrong
        assert raised == fx["check_false_positives"]
    else:
        assert raised == fx["expected_flags"]


@pytest.mark.parametrize("path", [p for p in FILES if is_real(load(p))], ids=lambda p: p.stem)
def test_real_fixture_alternatives_point_at_fields(path):
    fx = load(path)
    for field_path in fx.get("alternatives", {}):
        try:
            get_at(fx["expected"], field_path)
        except InvalidPath:
            pytest.fail(f"alternative for unknown field {field_path!r}")
    for key in ("url", "license", "original_file", "sha256"):
        assert fx["source"][key], f"source.{key} missing (attribution / reproducibility)"
    # only labels a human checked against the photo may be used to score the model
    assert fx.get("verified_by"), "real fixture not human-verified yet (run the label review)"
    # tax_breakdown was labeled later (prompt v3) and reviewed on its own
    assert fx.get("tax_breakdown_verified_by"), "tax_breakdown not human-verified yet"


def test_ids_unique_and_duplicates_point_somewhere():
    fixtures = [load(p) for p in FILES]
    ids = [f["id"] for f in fixtures]
    assert len(ids) == len(set(ids))
    for f in fixtures:
        if f.get("duplicate_of"):
            assert f["duplicate_of"] in ids
