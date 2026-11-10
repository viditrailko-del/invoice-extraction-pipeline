"""Guards the eval fixtures (answer keys) against drift.

If someone changes the schema or a check, these tests fail until the fixtures are
regenerated (python -m scripts.make_fixtures) or the change is reconsidered.
Covers every fixture under eval/fixtures/, synthetic or hand-labeled.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.checks.runner import run_checks
from app.schemas.extraction import ExtractedInvoice

FIXTURES = Path(__file__).parents[1] / "eval" / "fixtures"
FILES = sorted(FIXTURES.rglob("*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixtures_exist():
    assert len(FILES) >= 20


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_fixture_is_valid(path):
    fx = load(path)
    assert (path.parent / fx["file"]).is_file(), "document file missing"
    # answer key must satisfy the same schema the LLM must satisfy (and round-trip)
    inv = ExtractedInvoice.model_validate(fx["expected"])
    assert inv.model_dump(mode="json") == fx["expected"]
    # the checks, run on the TRUE data, raise exactly the expected flags
    if "expected_flags" in fx:
        flags = run_checks(inv, date.fromisoformat(fx["as_of"]))
        assert [{"code": f.code, "field_path": f.field_path} for f in flags] == fx["expected_flags"]


def test_ids_unique_and_duplicates_point_somewhere():
    fixtures = [load(p) for p in FILES]
    ids = [f["id"] for f in fixtures]
    assert len(ids) == len(set(ids))
    for f in fixtures:
        if f.get("duplicate_of"):
            assert f["duplicate_of"] in ids
