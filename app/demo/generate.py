""""Generate an invoice" for the public demo: a fresh synthetic PDF, optionally with one planted mistake.

Same generator as the eval fixtures (app/invoicegen.py): the data is decided first and the PDF drawn from it, so
the demo knows exactly what is printed (`printed`) and can show it beside what Gemini read. Before returning, the
real checks must raise exactly the planted problem's codes on the printed data (the fixtures' self-check), so
"we planted X" is never a claim the checks don't back.

Each call draws new random choices (vendor, layout, lines, numbers), so the file is new every time and costs one
live extraction.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date
from random import Random
from typing import Any

from app.checks.runner import run_checks
from app.invoicegen import LAYOUTS, PLANTS, VENDORS, Spec, build, render_pdf
from app.schemas.extraction import ExtractedInvoice

# What a visitor can plant, in plain words for the page. (tax_twice needs a tax-included single-rate invoice.)
PLANT_CHOICES = {
    "total": "The printed total is wrong",
    "line_math": "One line's amount isn't quantity x price",
    "subtotal": "The subtotal doesn't match the lines",
    "tax_row": "One VAT row of a receipt is wrong",
    "due_before": "Due date before the invoice date",
    "old_date": "Dated three years ago",
    "no_number": "No invoice number",
}
SINGLE_RATE = [k for k, v in VENDORS.items() if not v.rates]


@dataclass(frozen=True)
class Generated:
    pdf: bytes
    filename: str
    printed: dict[str, Any]  # ExtractedInvoice as printed (the planted mistake included)
    planted_codes: list[str]  # the check codes the planted mistake must trigger
    description: str


MAX_DRAWS = 20


def generate(plant: str | None, today: date, seed: str | None = None) -> Generated:
    """A new invoice. A draw whose checks don't raise exactly the planted codes is discarded and drawn again:
    rarely, a planted wrong total equals the discount, and "subtotal already after discount" is a reading the
    total check accepts on purpose."""
    if plant is not None and plant not in PLANT_CHOICES:
        raise ValueError(f"unknown plant {plant!r}")
    for draw in range(MAX_DRAWS):
        if (g := _draw(plant, today, f"{seed}-{draw}" if seed else secrets.token_hex(8))) is not None:
            return g
    raise RuntimeError(f"generator: no consistent invoice in {MAX_DRAWS} draws (plant {plant})")


def _draw(plant: str | None, today: date, seed: str) -> Generated | None:
    rng = Random(f"demo-{seed}")
    spec = _spec(plant, rng)
    inv = build(spec, rng, as_of=today)
    if plant:
        PLANTS[plant][0](inv, rng)
    printed = inv.expected()

    codes = sorted(PLANTS[plant][1]) if plant else []
    raised = sorted({f.code for f in run_checks(ExtractedInvoice.model_validate(printed), today)})
    if raised != codes:
        return None

    v = inv.vendor
    kind = "receipt" if spec.tax_inclusive and v.rates else "invoice"
    return Generated(
        pdf=render_pdf(inv, spec.layout),
        filename=f"generated-{seed}.pdf",
        printed=printed,
        planted_codes=codes,
        description=f"{v.name} ({v.currency}), {kind}, {len(inv.lines)} lines, {spec.layout} layout",
    )


def _spec(plant: str | None, rng: Random) -> Spec:
    if plant == "tax_row":  # needs a printed tax table with bases: the Portuguese receipt
        vendor = "pt"
    elif plant in ("subtotal", "total"):  # single rate: on a tax table a wrong total also breaks bases + tax
        vendor = rng.choice(SINGLE_RATE)
    else:
        vendor = rng.choice(list(VENDORS))
    v = VENDORS[vendor]
    receipt = vendor == "pt"  # a till receipt: tax included, no due date, no buyer
    layouts = [k for k in LAYOUTS if not (v.rates and k == "band")]  # band overprints long vendor names
    discount = rng.choice([None, None, 5, 10]) if not v.rates and plant != "subtotal" else None
    return Spec(
        id="demo", vendor=vendor, layout=rng.choice(layouts), n_lines=rng.randint(2, 6),
        discount_pct=discount, negative_discount=bool(discount) and rng.random() < 0.5,
        tax_inclusive=receipt, no_due=receipt, no_bill_to=receipt,
    )
