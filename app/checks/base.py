"""Shared types for deterministic checks.

A check is a pure function: (ExtractedInvoice) -> list[Flag]. No DB, no network,
so every check is trivially unit-testable and re-runnable after a human correction.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

# Printed money is rounded to cents, so allow one cent of slack in comparisons.
TOLERANCE = Decimal("0.01")
CENT = Decimal("0.01")

FlagSeverity = Literal["warning", "error"]


@dataclass(frozen=True)
class Flag:
    code: str  # stable check identifier, e.g. "total_mismatch"
    field_path: str  # e.g. "line_items[2].amount"
    reason: str
    severity: FlagSeverity


def to_cents(value: Decimal) -> Decimal:
    """Round like an invoice does (half up, not banker's rounding)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money_equal(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE
