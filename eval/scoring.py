"""Scoring one extraction against its label. Pure functions: no API, no DB, no files.

Three questions, from narrow to the one that matters most:
  1. Fields: is each value right?            -> FieldResult per field
  2. Flags:  did the checks raise what they should, and only that?
  3. Decision: would the pipeline auto-approve an invoice it got wrong?
     ("unsafe approval": the costly failure the whole reliability layer exists to stop)
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.checks.base import TOLERANCE
from app.checks.duplicates import normalize_text
from app.checks.runner import run_checks
from app.schemas.extraction import ExtractedInvoice
from eval.dataset import FlagKey

HEADER_FIELDS = (
    "invoice_number",
    "invoice_date",
    "due_date",
    "vendor.name",
    "vendor.address",
    "vendor.tax_id",
    "bill_to_name",
    "currency",
    "subtotal",
    "tax",
    "discount",
    "total",
    "prices_include_tax",
)
LINE_FIELDS = ("description", "quantity", "unit_price", "amount")
LINE_COUNT = "line_items.count"

NUMERIC = {"subtotal", "tax", "discount", "total", "quantity", "unit_price", "amount"}
EXACT = {"invoice_date", "due_date", "currency", "prices_include_tax"}

# Wrong values here change what gets paid, to whom, or when. A wrong one must never be
# auto-approved. Free text (descriptions, addresses, buyer name) is scored but not
# critical: a misread line description doesn't move money.
CRITICAL = {
    "invoice_number",
    "invoice_date",
    "due_date",
    "vendor.name",
    "vendor.tax_id",
    "currency",
    "subtotal",
    "tax",
    "discount",
    "total",
    "line_items[].amount",
    LINE_COUNT,
}

Outcome = Literal["correct", "wrong", "missing", "hallucinated", "failed"]
Decision = Literal["correct_approval", "caught", "needless_review", "unsafe_approval"]


@dataclass(frozen=True)
class FieldResult:
    path: str  # "total", "line_items[2].amount", "line_items.count"
    outcome: Outcome
    expected: Any
    actual: Any

    @property
    def ok(self) -> bool:
        return self.outcome == "correct"

    @property
    def field(self) -> str:
        """Path with the line index removed, for per-field totals: line_items[].amount."""
        return re.sub(r"\[\d+\]", "[]", self.path)

    @property
    def critical(self) -> bool:
        return self.field in CRITICAL


# --- 1. fields ----------------------------------------------------------------


def normalize_free_text(value: str) -> str:
    """Case, spacing, punctuation and accents don't count: 'JOÃO, Lda.' == 'joao lda'."""
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return normalize_text(ascii_text)


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def values_match(path: str, expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    leaf = path.rsplit(".", 1)[-1]
    if leaf in NUMERIC:
        e, a = _to_decimal(expected), _to_decimal(actual)
        if e is None or a is None:
            return False
        if leaf == "discount":  # printed as -5.00 or 5.00: both mean the same discount
            e, a = abs(e), abs(a)
        return abs(e - a) <= TOLERANCE
    if leaf in EXACT:
        return str(expected) == str(actual)
    return normalize_free_text(str(expected)) == normalize_free_text(str(actual))


def score_field(path: str, expected: Any, actual: Any, alternatives: list[Any]) -> FieldResult:
    accepted = [expected, *alternatives]
    if any(values_match(path, ok, actual) for ok in accepted):
        outcome: Outcome = "correct"
    elif actual is None:
        outcome = "missing"  # the document has it, the model returned null
    elif expected is None:
        outcome = "hallucinated"  # the document doesn't have it, the model invented one
    else:
        outcome = "wrong"
    return FieldResult(path, outcome, expected, actual)


def get_path(data: dict[str, Any], path: str) -> Any:
    """get_path(inv, 'vendor.name') / get_path(inv, 'line_items[2].amount')."""
    value: Any = data
    for part in path.split("."):
        m = re.fullmatch(r"(\w+)\[(\d+)\]", part)
        if m:
            items = value.get(m.group(1)) or []
            idx = int(m.group(2))
            value = items[idx] if idx < len(items) else None
        else:
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            return None
    return value


def score_fields(
    expected: dict[str, Any], actual: dict[str, Any] | None, alternatives: dict[str, list[Any]]
) -> list[FieldResult]:
    """Score every header field, every line item field (matched by position), and the
    line count. A failed extraction (actual=None) scores every field as 'failed'."""
    exp_lines = expected.get("line_items") or []
    act_lines = (actual or {}).get("line_items") or []
    paths = list(HEADER_FIELDS) + [
        f"line_items[{i}].{f}" for i in range(max(len(exp_lines), len(act_lines))) for f in LINE_FIELDS
    ]

    results = []
    for path in paths:
        exp_value = get_path(expected, path)
        if actual is None:
            results.append(FieldResult(path, "failed", exp_value, None))
        else:
            results.append(
                score_field(path, exp_value, get_path(actual, path), alternatives.get(path, []))
            )
    count_outcome: Outcome = (
        "failed" if actual is None else "correct" if len(exp_lines) == len(act_lines) else "wrong"
    )
    results.append(FieldResult(LINE_COUNT, count_outcome, len(exp_lines), len(act_lines)))
    return results


# --- 2. flags -----------------------------------------------------------------


def predicted_flags(extracted: ExtractedInvoice, as_of: date) -> set[FlagKey]:
    """The same pure checks the API runs, at the fixture's as_of date (not today),
    so date checks give the same answer whenever the eval runs."""
    return {(f.code, f.field_path) for f in run_checks(extracted, as_of)}


@dataclass(frozen=True)
class FlagResult:
    matched: set[FlagKey]  # expected and raised
    missed: set[FlagKey]  # expected, not raised
    on_bad_extraction: set[FlagKey]  # not expected, but the extraction WAS wrong: useful
    false_alarms: set[FlagKey]  # not expected, extraction was right: pure noise


def score_flags(
    expected: set[FlagKey], predicted: set[FlagKey], extraction_wrong: bool
) -> FlagResult:
    unexpected = predicted - expected
    return FlagResult(
        matched=predicted & expected,
        missed=expected - predicted,
        on_bad_extraction=unexpected if extraction_wrong else set(),
        false_alarms=set() if extraction_wrong else unexpected,
    )


# --- 3. the approve / review decision ------------------------------------------


def decide(*, should_review: bool, flagged: bool) -> Decision:
    """should_review: the document has a real problem OR we extracted it wrong.
    flagged: the pipeline would send it to a human (any flag, or extraction failed)."""
    if should_review:
        return "caught" if flagged else "unsafe_approval"
    return "needless_review" if flagged else "correct_approval"
