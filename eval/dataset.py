"""Loading the labeled fixtures (eval/fixtures/{synthetic,real}/<id>.json + document)."""

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.checks.duplicates import DUPLICATE_CODE

EVAL_DIR = Path(__file__).parent
FIXTURES_DIR = EVAL_DIR / "fixtures"
RESULTS_DIR = EVAL_DIR / "results"
SPLITS = ("synthetic", "real")

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

FlagKey = tuple[str, str]  # (code, field_path): how flags are compared


@dataclass(frozen=True)
class Fixture:
    id: str
    split: str
    file_path: Path
    label: dict[str, Any]

    @property
    def mime_type(self) -> str:
        return MIME_TYPES[self.file_path.suffix.lower()]

    @property
    def expected(self) -> dict[str, Any]:
        return self.label["expected"]

    @property
    def alternatives(self) -> dict[str, list[Any]]:
        return self.label.get("alternatives", {})

    @property
    def as_of(self) -> date:
        return date.fromisoformat(self.label["as_of"])

    @property
    def duplicate_of(self) -> str | None:
        return self.label.get("duplicate_of")

    @property
    def expected_flags(self) -> set[FlagKey]:
        flags = {(f["code"], f["field_path"]) for f in self.label["expected_flags"]}
        if self.duplicate_of:
            # A re-scan should be caught by duplicate detection (which needs the DB in the
            # API, so it isn't part of run_checks and isn't in the label's expected_flags).
            flags.add((DUPLICATE_CODE, "invoice_number"))
        return flags

    @property
    def known_check_false_positives(self) -> set[FlagKey]:
        return {(f["code"], f["field_path"]) for f in self.label.get("check_false_positives", [])}


def load_fixtures(only: set[str] | None = None) -> list[Fixture]:
    """All fixtures sorted by id (order matters: duplicates must come after originals)."""
    fixtures = []
    for split in SPLITS:
        for label_path in sorted((FIXTURES_DIR / split).glob("*.json")):
            label = json.loads(label_path.read_text(encoding="utf-8"))
            if only and label["id"] not in only:
                continue
            fixtures.append(Fixture(label["id"], split, label_path.parent / label["file"], label))
    return fixtures
