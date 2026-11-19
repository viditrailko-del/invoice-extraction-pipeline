"""Generate synthetic eval fixtures: invoice files + their exact expected JSON.

    docker compose run --rm -T api uv run --frozen python -m scripts.make_fixtures

Why synthetic: the data is decided FIRST and the document is drawn FROM it, so the
answer key is exact by construction (no hand-labeling mistakes). It also lets us
plant specific errors (wrong totals, bad dates...) to measure whether the Phase 3
checks catch them, which real invoices almost never exercise.

Each fixture = eval/fixtures/synthetic/<id>.<pdf|png|jpg> + <id>.json:
    expected        ExtractedInvoice exactly as PRINTED (wrong numbers included)
    expected_flags  what run_checks() must raise on that data, evaluated at `as_of`
    planted         which errors were planted on purpose (empty = clean invoice)
    duplicate_of    fixture id this is a re-scan of (business duplicate), or null

Deterministic: a fixed seed per fixture id and reportlab's invariant mode, so
re-running produces byte-identical files and no git noise. The invoice model and the
PDF layouts are in app/invoicegen.py (shared with the public demo's generator).
"""

import json
import random
import shutil
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageFilter

from app.checks.runner import run_checks
from app.invoicegen import AS_OF, PLANTS, Inv, Spec, build, fmt_money, fmt_num, pct, render_pdf
from app.schemas.extraction import ExtractedInvoice

OUT_DIR = Path("eval/fixtures/synthetic")
SEED = 20261110


# --- output -----------------------------------------------------------------------
# The invoice model, vendors, planted errors and PDF layouts are in app/invoicegen.py.


def rasterize(pdf: bytes, rng: random.Random, scan: bool) -> Image.Image:
    page = pdfium.PdfDocument(pdf)[0]
    img = page.render(scale=150 / 72).to_pil().convert("RGB")
    if not scan:
        return img
    # Looks like a cheap office scan: grey, slightly rotated, blurred, noisy.
    img = img.convert("L").rotate(rng.uniform(-2.0, 2.0), resample=Image.Resampling.BICUBIC,
                                  expand=True, fillcolor=235)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    noise = Image.frombytes("L", img.size, rng.randbytes(img.size[0] * img.size[1]))
    return Image.blend(img, noise, 0.07)


def pdf_text(pdf: bytes) -> str:
    doc = pdfium.PdfDocument(pdf)
    return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))


# --- the fixture set -----------------------------------------------------------------

SPECS = [
    # clean: variety of layouts, languages, number/date formats
    Spec("syn-001", "uk", "classic", 3, note="baseline UK invoice"),
    Spec("syn-002", "us", "band", 5, discount_pct=10, note="US date format, discount printed positive"),
    Spec("syn-003", "de", "classic", 4, note="German labels, 1.234,56 numbers, dd.mm.yyyy"),
    Spec("syn-004", "in", "minimal", 6, note="Indian lakh grouping, vendor only in footer"),
    Spec("syn-005", "fr", "band", 4, discount_pct=5, negative_discount=True, note="French labels, discount printed negative"),
    Spec("syn-006", "ca", "classic", 3, no_due=True, note="ISO dates, CAD, no due date"),
    Spec("syn-007", "us_notax", "minimal", 3, note="no tax, no tax id, long date format"),
    Spec("syn-008", "no", "band", 2, note="NOK, space thousands separator"),
    Spec("syn-009", "us", "classic", 40, note="multi-page (2 pages)"),
    Spec("syn-010", "uk", "minimal", 1, no_bill_to=True, note="single line, no bill-to"),
    Spec("syn-011", "de", "band", 5, fmt="scan", note="scanned German invoice"),
    Spec("syn-012", "in", "classic", 4, fmt="scan", note="scanned Indian invoice"),
    Spec("syn-013", "fr", "minimal", 3, fmt="png", note="clean PNG image"),
    Spec("syn-014", "uk", "band", 12, fmt="png", note="12 lines, fractional hours, PNG"),
    Spec("syn-015", "ca", "minimal", 4, discount_pct=15, note="discount, minimal layout"),
    Spec("syn-016", "us_notax", "classic", 5, credit_note=True, note="credit note: negative quantities and totals"),
    # planted errors: the checks must flag these
    Spec("syn-017", "uk", "classic", 4, plant=("line_math",), note="one line amount != qty x price"),
    Spec("syn-018", "us", "band", 5, plant=("subtotal",), note="subtotal 100.00 too high"),
    Spec("syn-019", "de", "minimal", 3, plant=("total",), note="printed total wrong"),
    Spec("syn-020", "fr", "classic", 3, plant=("due_before",), note="due date before invoice date"),
    Spec("syn-021", "ca", "band", 3, plant=("old_date",), note="invoice dated 3 years ago"),
    Spec("syn-022", "uk", "minimal", 2, plant=("no_number",), note="no invoice number printed"),
    Spec("syn-023", "in", "band", 5, fmt="scan", plant=("total",), note="scanned, wrong total"),
    Spec("syn-024", "no", "classic", 4, plant=("line_math", "due_before"), note="two problems at once"),
    # business duplicate: same invoice as syn-001, re-scanned
    Spec("syn-025", "uk", "classic", 3, fmt="scan", duplicate_of="syn-001", note="re-scan of syn-001"),
    # tax already included in prices (EU shop-receipt style): no subtotal, tax "of which"
    Spec("syn-026", "uk", "minimal", 3, tax_inclusive=True, note="prices include VAT"),
    Spec("syn-027", "de", "band", 4, fmt="scan", tax_inclusive=True, note="German, inkl. MwSt., scanned"),
    Spec("syn-028", "fr", "classic", 3, tax_inclusive=True, plant=("tax_twice",),
         note="TVA included, but added to the total again"),
    # several tax rates with a printed tax table (rate / base / tax per row)
    Spec("syn-029", "pt", "minimal", 5, tax_inclusive=True, no_due=True, no_bill_to=True,
         note="Portuguese receipt, IVA 23% + 6% table, prices include tax"),
    Spec("syn-030", "de_mixed", "classic", 4, note="German B2B, MwSt. 19% + 7% table, tax added"),
    # (not "band": its big title overprints a long vendor name)
    Spec("syn-031", "pt", "classic", 4, fmt="scan", tax_inclusive=True, no_due=True, no_bill_to=True,
         plant=("tax_row",), note="scanned receipt, 23% row's IVA 0.40 short (like pt-02's smudge)"),
]


def make_inv(spec: Spec) -> tuple[Inv, random.Random]:
    # A duplicate reuses its original's seed so the data is identical.
    rng = random.Random(f"{SEED}-{spec.duplicate_of or spec.id}")
    base = SPECS_BY_ID[spec.duplicate_of] if spec.duplicate_of else spec
    inv = build(base, rng)
    for name in base.plant:
        PLANTS[name][0](inv, rng)
    return inv, rng


SPECS_BY_ID = {s.id: s for s in SPECS}


def make_fixture(spec: Spec) -> dict:
    inv, rng = make_inv(spec)
    layout = SPECS_BY_ID[spec.duplicate_of].layout if spec.duplicate_of else spec.layout
    expected = inv.expected()

    # Self-check 1: the real checks must raise exactly the planted problems.
    flags = run_checks(ExtractedInvoice.model_validate(expected), AS_OF)
    planted_codes = {c for p in SPECS_BY_ID.get(spec.duplicate_of, spec).plant for c in PLANTS[p][1]}
    if inv.credit_note:
        planted_codes.add("negative_amount")
    got = {f.code for f in flags}
    assert got == planted_codes, f"{spec.id}: checks raised {got}, planted {planted_codes}"

    # Self-check 2: what the answer key says is really printed on the page.
    pdf = render_pdf(inv, layout)
    text = pdf_text(pdf)
    must_appear = [fmt_money(inv.total, inv.vendor), inv.vendor.name]
    if inv.multi_rate:  # every tax-table number the label claims is printed
        s = inv.vendor.number_style
        for row in inv.tax_rows or []:
            must_appear += [fmt_num(row.base, s), fmt_num(row.amount, s), f"{fmt_num(pct(row.rate), s, places=0)}%"]
    if inv.number:
        must_appear.append(inv.number)
    for s in must_appear:
        assert s in text, f"{spec.id}: {s!r} not found in rendered PDF"

    if spec.fmt == "pdf":
        name = f"{spec.id}.pdf"
        (OUT_DIR / name).write_bytes(pdf)
    else:
        img = rasterize(pdf, rng, scan=spec.fmt == "scan")
        name = f"{spec.id}.jpg" if spec.fmt == "scan" else f"{spec.id}.png"
        img.save(OUT_DIR / name, **({"quality": 60} if spec.fmt == "scan" else {"optimize": True}))

    fixture = {
        "id": spec.id,
        "file": name,
        "source": "synthetic",
        "as_of": AS_OF.isoformat(),
        "description": spec.note,
        "layout": layout,
        "planted": sorted(planted_codes),
        "duplicate_of": spec.duplicate_of,
        "expected": expected,
        "expected_flags": [{"code": f.code, "field_path": f.field_path} for f in flags],
    }
    (OUT_DIR / f"{spec.id}.json").write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return fixture


def main() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)  # generated dir: always rebuilt from SPECS
    OUT_DIR.mkdir(parents=True)
    for spec in SPECS:
        f = make_fixture(spec)
        status = ", ".join(f["planted"]) or "clean"
        print(f"{f['id']}  {f['file']:<12} {f['layout']:<8} {status:<40} {f['description']}")
    print(f"\n{len(SPECS)} fixtures written to {OUT_DIR}/")


if __name__ == "__main__":
    main()
