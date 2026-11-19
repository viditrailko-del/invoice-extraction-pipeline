"""Synthetic invoices: the data first, then a PDF drawn from it, so the answer key is exact by construction.

Used by scripts/make_fixtures.py (the eval fixtures, rendered to images there) and by the public demo's
"generate an invoice" (app/demo/generate.py). Only needs reportlab: rasterizing to scans lives in the script.

Deterministic: same Spec + same seeded Random -> byte-identical PDF (reportlab's invariant mode).
"""

import io
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from app.checks.base import to_cents
from app.schemas.extraction import ExtractedInvoice

AS_OF = date(2026, 11, 10)  # the "today" the eval fixtures are evaluated against
W, H = A4
ZERO = Decimal("0")

# --- vendors, buyers, catalog ---------------------------------------------------


@dataclass(frozen=True)
class Vendor:
    name: str
    address: tuple[str, ...]
    tax_label: str | None
    tax_id: str | None
    currency: str
    symbol: str
    symbol_after: bool
    number_style: str  # en | eu | eu_space | in
    date_style: str  # dmy | mdy | dmy_dot | iso | long
    tax_rate: Decimal
    tax_name: str | None
    lang: str  # en | de | fr
    number_fmt: str  # format for the invoice number
    catalog: str
    # Several tax rates (e.g. 23% and 6%): each line has its own rate and the document
    # prints a tax table (rate / base / tax per row). Empty = the single `tax_rate`.
    rates: tuple[Decimal, ...] = ()


# Tax IDs are fake but VALID (correct check digits, made with python-stdnum):
# checks.tax_id would flag an invalid one, which no real vendor prints.
VENDORS = {
    "uk": Vendor("Northwind Cloud Services Ltd.", ("12 Harbour Street", "Bristol BS1 4XY, United Kingdom"),
                 "VAT No", "GB123456727", "GBP", "£", False, "en", "dmy", Decimal("0.20"), "VAT 20%",
                 "en", "NW-{y}-{n:04d}", "cloud"),
    "us": Vendor("Brightline Design Studio LLC", ("480 Mission Street, Suite 300", "San Francisco, CA 94105, USA"),
                 "EIN", "94-3812775", "USD", "$", False, "en", "mdy", Decimal("0.0875"), "Sales tax 8.75%",
                 "en", "INV{n:06d}", "design"),
    "de": Vendor("Kessler Maschinenbau GmbH", ("Industriestraße 14", "70565 Stuttgart, Deutschland"),
                 "USt-IdNr.", "DE287451900", "EUR", "€", True, "eu", "dmy_dot", Decimal("0.19"), "MwSt. 19%",
                 "de", "RE-{y}{n:05d}", "machinery"),
    "in": Vendor("Sahyadri Software Solutions Pvt Ltd", ("Plot 22, Hinjewadi Phase 2", "Pune 411057, Maharashtra, India"),
                 "GSTIN", "27AABCS1429B1ZU", "INR", "Rs.", False, "in", "dmy", Decimal("0.18"), "GST 18%",
                 "en", "SSS/{yy}-{yy1}/{n:03d}", "software_in"),
    "fr": Vendor("Atelier Lumière SARL", ("8 Rue des Lilas", "69003 Lyon, France"),
                 "N° TVA", "FR40303265045", "EUR", "€", True, "eu_space", "dmy", Decimal("0.20"), "TVA 20%",
                 "fr", "F{y}/{n:04d}", "studio_fr"),
    "ca": Vendor("Maple Leaf Logistics Inc.", ("220 King St W", "Toronto, ON M5H 1K4, Canada"),
                 "GST/HST No", "123456782RT0001", "CAD", "CA$", False, "en", "iso", Decimal("0.13"), "HST 13%",
                 "en", "MLL-{n:05d}", "logistics"),
    "us_notax": Vendor("Harbor & Pine Consulting", ("77 Pine Avenue", "Portland, OR 97204, USA"),
                       None, None, "USD", "$", False, "en", "long", ZERO, None,
                       "en", "HP-{y}-{n:03d}", "consulting"),
    "no": Vendor("Nordlys Data AS", ("Karl Johans gate 5", "0154 Oslo, Norway"),
                 "Org.nr", "NO987654325MVA", "NOK", "kr", False, "eu_space", "dmy_dot", Decimal("0.25"), "MVA 25%",
                 "en", "{n:05d}", "data_no"),
    # multi-rate: a Portuguese shop receipt (tax included) and a German B2B food
    # wholesaler (tax added), each printing a tax table
    "pt": Vendor("Mercearia Flor do Mondego, Lda.", ("Rua Ferreira Borges 41", "3000-180 Coimbra, Portugal"),
                 "NIF", "PT509876544", "EUR", "€", True, "eu", "dmy", Decimal("0.23"), "IVA",
                 "pt", "FS {y}/{n:05d}", "grocery_pt", rates=(Decimal("0.23"), Decimal("0.06"))),
    "de_mixed": Vendor("Rheinland Gastro-Großhandel GmbH", ("Hafenstraße 9", "50678 Köln, Deutschland"),
                       "USt-IdNr.", "DE314159262", "EUR", "€", True, "eu", "dmy_dot", Decimal("0.19"), "MwSt.",
                       "de", "GH-{y}-{n:05d}", "food_de", rates=(Decimal("0.19"), Decimal("0.07"))),
}

BUYERS = [
    "Acme Analytics Pvt Ltd", "Bluewater Retail Group", "Contoso Pharmaceuticals",
    "Greenfield Schools Trust", "Orion Health Partners", "Pacific Rim Traders",
    "Müller & Söhne KG", "Rivière Bleue Événements", "Fjordline Retail AS",
]

# (description, qty kind, qty range, unit price range)
CATALOG: dict[str, list[tuple[str, str, tuple[int, int], tuple[str, str]]]] = {
    "cloud": [("Cloud hosting - {month}", "int", (1, 1), ("300", "900")),
              ("Support hours", "half", (2, 20), ("65", "120")),
              ("SSL certificate", "int", (1, 3), ("39.50", "39.50")),
              ("Managed backup (TB)", "int", (1, 10), ("12", "25")),
              ("Domain renewal", "int", (1, 5), ("14.99", "14.99")),
              ("Database instance db.m5", "int", (1, 4), ("120", "260"))],
    "design": [("Brand identity workshop", "int", (1, 1), ("1200", "2400")),
               ("Illustration", "int", (1, 6), ("150", "350")),
               ("Revisions (hours)", "half", (1, 12), ("75", "95")),
               ("Print-ready files", "int", (1, 1), ("120", "120")),
               ("Stock photo license", "int", (1, 8), ("29", "79"))],
    "machinery": [("Hydraulikzylinder HZ-40", "int", (1, 6), ("180", "420")),
                  ("Dichtungssatz", "int", (2, 20), ("8.50", "24")),
                  ("Montage (Std.)", "half", (1, 16), ("68", "92")),
                  ("Frachtkosten", "int", (1, 1), ("45", "120")),
                  ("Kugellager 6204-2RS", "int", (5, 50), ("3.20", "6.80"))],
    "software_in": [("Annual maintenance contract", "int", (1, 1), ("85000", "240000")),
                    ("Developer hours", "int", (10, 160), ("1200", "2800")),
                    ("Cloud migration services", "int", (1, 1), ("45000", "150000")),
                    ("Training session", "int", (1, 5), ("15000", "35000"))],
    "studio_fr": [("Conception graphique", "int", (1, 3), ("400", "900")),
                  ("Séance photo (demi-journée)", "int", (1, 2), ("350", "650")),
                  ("Impression affiches A2", "int", (10, 200), ("2.40", "4.90")),
                  ("Frais de déplacement", "int", (1, 1), ("60", "180"))],
    "logistics": [("Pallet storage (per pallet/week)", "int", (4, 60), ("6.50", "11")),
                  ("FTL Toronto-Montreal", "int", (1, 3), ("950", "1450")),
                  ("Fuel surcharge", "int", (1, 1), ("85", "240")),
                  ("Customs brokerage", "int", (1, 2), ("125", "175"))],
    "consulting": [("Strategy consulting (hours)", "half", (4, 40), ("150", "220")),
                   ("Workshop facilitation", "int", (1, 2), ("1800", "2600")),
                   ("Travel expenses", "int", (1, 1), ("120", "640"))],
    "data_no": [("Data pipeline subscription", "int", (1, 1), ("4500", "9800")),
                ("Analytics seats", "int", (5, 40), ("290", "450")),
                ("Onboarding", "int", (1, 1), ("6500", "12000"))],
    # multi-rate catalogs: the item's rate is in RATE_OF (index into Vendor.rates)
    "grocery_pt": [("Detergente roupa 3L", "int", (1, 2), ("6.49", "9.99")),
                   ("Pilhas AA (pack 4)", "int", (1, 2), ("3.99", "5.49")),
                   ("Pão de forma", "int", (1, 3), ("1.29", "2.19")),
                   ("Leite meio-gordo 1L", "int", (2, 6), ("0.79", "0.99")),
                   ("Azeite virgem extra 0,75L", "int", (1, 2), ("5.99", "8.49"))],
    "food_de": [("Spülmaschinentabs (200 St.)", "int", (1, 4), ("24.90", "39.90")),
                ("Mineralwasser Kiste 12x1L", "int", (5, 30), ("4.20", "6.80")),
                ("Kaffeebohnen 1 kg", "int", (2, 12), ("14.50", "22.90")),
                ("Weizenmehl Type 405, 25 kg", "int", (1, 6), ("17.80", "26.40"))],
}
RATE_OF = {  # 0 = standard rate, 1 = reduced rate
    "Detergente roupa 3L": 0, "Pilhas AA (pack 4)": 0, "Pão de forma": 1,
    "Leite meio-gordo 1L": 1, "Azeite virgem extra 0,75L": 1,
    "Spülmaschinentabs (200 St.)": 0, "Mineralwasser Kiste 12x1L": 0,
    "Kaffeebohnen 1 kg": 1, "Weizenmehl Type 405, 25 kg": 1,
}

LABELS = {
    "en": dict(invoice="INVOICE", credit="CREDIT NOTE", number="Invoice #", date="Date", due="Due",
               bill_to="Bill to", desc="Description", qty="Qty", unit="Unit price", amount="Amount",
               subtotal="Subtotal", discount="Discount", total="Total due", cont="(continued)",
               incl="Includes {}", rate="Rate", base="Net", tax_amt="Tax"),
    "pt": dict(invoice="FATURA SIMPLIFICADA", credit="NOTA DE CRÉDITO", number="N.º", date="Data", due="Vencimento",
               bill_to="Cliente", desc="Descrição", qty="Qtd", unit="Preço", amount="Valor",
               subtotal="Subtotal", discount="Desconto", total="Total", cont="(continuação)",
               incl="IVA incluído", rate="Taxa", base="Base", tax_amt="IVA"),
    "de": dict(invoice="RECHNUNG", credit="GUTSCHRIFT", number="Rechnungsnr.", date="Datum", due="Fällig am",
               bill_to="Rechnung an", desc="Beschreibung", qty="Menge", unit="Einzelpreis", amount="Betrag",
               subtotal="Zwischensumme", discount="Rabatt", total="Gesamtbetrag", cont="(Fortsetzung)",
               incl="enthaltene {}", rate="Satz", base="Netto", tax_amt="MwSt."),
    "fr": dict(invoice="FACTURE", credit="AVOIR", number="N° de facture", date="Date", due="Échéance",
               bill_to="Facturé à", desc="Désignation", qty="Qté", unit="Prix unitaire", amount="Montant",
               subtotal="Sous-total HT", discount="Remise", total="Total TTC", cont="(suite)",
               incl="dont {}", rate="Taux", base="Base HT", tax_amt="TVA"),
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


# --- the invoice as data ----------------------------------------------------------


@dataclass
class Line:
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    rate: Decimal | None = None  # multi-rate vendors only; not an extracted field


@dataclass
class TaxRow:
    rate: Decimal  # fraction: 0.23
    base: Decimal | None  # None = not printed (single-rate docs show only "VAT 20%: x")
    amount: Decimal


def pct(rate: Decimal) -> Decimal:
    """0.23 -> 23, 0.0875 -> 8.75 (as printed; never '2E+1')."""
    p = rate * 100
    return p.quantize(Decimal(1)) if p == p.to_integral() else p.normalize()


@dataclass
class Inv:
    vendor: Vendor
    number: str | None
    issued: date
    due: date | None
    bill_to: str | None
    lines: list[Line]
    discount_pct: int | None = None
    negative_discount: bool = False  # print discount as -12.00 instead of 12.00
    credit_note: bool = False
    tax_inclusive: bool = False  # receipt style: prices include tax, tax shown "of which"
    subtotal: Decimal | None = ZERO  # None = no subtotal line printed
    discount: Decimal | None = None  # as printed, sign included
    tax: Decimal | None = None
    total: Decimal = ZERO
    tax_rows: list[TaxRow] | None = None

    @property
    def multi_rate(self) -> bool:
        return len(self.vendor.rates) > 1

    def expected(self) -> dict:
        """The answer key, validated by the same schema Gemini must satisfy."""
        v = self.vendor
        return ExtractedInvoice.model_validate({
            "invoice_number": self.number,
            "invoice_date": self.issued,
            "due_date": self.due,
            "vendor": {"name": v.name, "address": ", ".join(v.address), "tax_id": v.tax_id},
            "bill_to_name": self.bill_to,
            "currency": v.currency,
            "line_items": [
                {"description": l.description, "quantity": l.quantity,
                 "unit_price": l.unit_price, "amount": l.amount}
                for l in self.lines
            ],
            "subtotal": self.subtotal,
            "tax": self.tax,
            "tax_breakdown": [
                {"rate": pct(r.rate), "base": r.base, "amount": r.amount} for r in self.tax_rows or []
            ],
            "discount": self.discount,
            "total": self.total,
            # no tax shown -> null; otherwise what the document's layout states
            "prices_include_tax": self.tax_inclusive if self.vendor.tax_rate else None,
        }).model_dump(mode="json")


def compute_totals(inv: Inv, subtotal: Decimal | None = None) -> None:
    """Totals the way the vendor's software would. `subtotal` overrides the sum
    (used to plant a vendor whose subtotal is wrong but everything after is consistent)."""
    if inv.multi_rate:
        compute_multi_rate_totals(inv)
        return
    base = subtotal if subtotal is not None else sum((l.amount for l in inv.lines), ZERO)
    disc = to_cents(base * inv.discount_pct / 100) if inv.discount_pct else ZERO
    inv.discount = (-disc if inv.negative_discount else disc) if inv.discount_pct else None
    rate = inv.vendor.tax_rate
    if inv.tax_inclusive:
        # Prices already contain the tax: lines add up to the total, tax is "of which".
        inv.subtotal = None
        inv.total = base - disc
        inv.tax = to_cents(inv.total * rate / (1 + rate)) if rate else None
    else:
        inv.subtotal = base
        inv.tax = to_cents((base - disc) * rate) if rate else None
        inv.total = base - disc + (inv.tax or ZERO)
    # The tax name carries the rate ("VAT 20%"), so the document shows one rate row
    # with its amount but no separate base.
    inv.tax_rows = [TaxRow(rate, None, inv.tax)] if inv.tax is not None else []


def compute_multi_rate_totals(inv: Inv) -> None:
    """One tax row per rate, computed per rate group like till software does.
    Included: the row's gross is split into base + tax. Added: tax on the net base."""
    assert not inv.discount_pct, "discounts on multi-rate documents aren't modeled"
    rows = []
    for rate in inv.vendor.rates:
        group = sum((l.amount for l in inv.lines if l.rate == rate), ZERO)
        if not group:
            continue
        if inv.tax_inclusive:
            tax = to_cents(group * rate / (1 + rate))
            rows.append(TaxRow(rate, group - tax, tax))
        else:
            rows.append(TaxRow(rate, group, to_cents(group * rate)))
    inv.tax_rows = rows
    inv.tax = sum((r.amount for r in rows), ZERO)
    lines_sum = sum((l.amount for l in inv.lines), ZERO)
    inv.discount = None
    if inv.tax_inclusive:
        inv.subtotal, inv.total = None, lines_sum
    else:
        inv.subtotal, inv.total = lines_sum, lines_sum + inv.tax


def unambiguous(d: date, step: int = 1) -> date:
    """Day > 12, so 13/10 vs 10/13 can't be misread: the label stays well-defined."""
    while d.day <= 12:
        d += timedelta(days=step)
    return d


def build(spec: "Spec", rng: random.Random, as_of: date = AS_OF) -> Inv:
    v = VENDORS[spec.vendor]
    issued = unambiguous(as_of - timedelta(days=rng.randint(5, 120)), step=-1)
    lines = []
    for k in range(spec.n_lines):
        items = CATALOG[v.catalog]
        if len(v.rates) > 1:  # alternate standard / reduced items so both rates appear
            items = [it for it in items if RATE_OF[it[0]] == k % 2]
        desc, kind, (qlo, qhi), (plo, phi) = rng.choice(items)
        qty = Decimal(rng.randint(qlo, qhi)) if kind == "int" else Decimal(rng.randint(2 * qlo, 2 * qhi)) / 2
        price = Decimal(rng.randint(int(Decimal(plo) * 100), int(Decimal(phi) * 100))) / 100
        if price >= 1000:
            price = Decimal(round(price, -2))  # big prices are round numbers in real life
        price = price.quantize(Decimal("0.01"))
        if spec.credit_note:
            qty = -qty
        month = MONTHS[(issued.month - 2) % 12]
        rate = v.rates[RATE_OF[desc]] if len(v.rates) > 1 else None
        lines.append(Line(desc.format(month=month), qty, price, to_cents(qty * price), rate))
    n = rng.randint(1, 9999)
    number = v.number_fmt.format(y=issued.year, yy=issued.year % 100, yy1=issued.year % 100 + 1, n=n)
    inv = Inv(
        vendor=v,
        number=number,
        issued=issued,
        due=None if spec.no_due else unambiguous(issued + timedelta(days=rng.choice([14, 21, 30, 45]))),
        bill_to=None if spec.no_bill_to else rng.choice(BUYERS),
        lines=lines,
        discount_pct=spec.discount_pct,
        negative_discount=spec.negative_discount,
        credit_note=spec.credit_note,
        tax_inclusive=spec.tax_inclusive,
    )
    compute_totals(inv)
    return inv


# --- planted errors -------------------------------------------------------------
# Each mutates the PRINTED data (so the answer key contains the wrong value, exactly
# as a perfect extractor would read it) and names the check code that must fire.


def plant_line_math(inv: Inv, rng: random.Random) -> None:
    i = rng.randrange(len(inv.lines))
    inv.lines[i].amount += Decimal("10.00")  # qty x price no longer matches
    compute_totals(inv)  # vendor summed the wrong amount: only the line is off


def plant_subtotal(inv: Inv, rng: random.Random) -> None:
    compute_totals(inv, subtotal=inv.subtotal + Decimal("100.00"))  # rest consistent with it


def plant_total(inv: Inv, rng: random.Random) -> None:
    inv.total += rng.choice([Decimal("-7.00"), Decimal("45.00"), Decimal("120.00")])


def plant_tax_twice(inv: Inv, rng: random.Random) -> None:
    inv.total += inv.tax or ZERO  # tax-included prices, but tax added on top again


def plant_tax_row(inv: Inv, rng: random.Random) -> None:
    """Like pt-02's smudge, but on the paper: the first tax row's amount is 0.40 short
    and the printed tax total was summed from the wrong row. Lines and total are right."""
    assert inv.tax_rows and inv.tax_rows[0].base is not None
    inv.tax_rows[0].amount -= Decimal("0.40")
    inv.tax = sum((r.amount for r in inv.tax_rows), ZERO)


def plant_due_before(inv: Inv, rng: random.Random) -> None:
    inv.due = unambiguous(inv.issued - timedelta(days=20), step=-1)


def plant_old_date(inv: Inv, rng: random.Random) -> None:
    inv.issued = unambiguous(inv.issued - timedelta(days=3 * 365), step=-1)
    if inv.due:
        inv.due = unambiguous(inv.issued + timedelta(days=30))


def plant_no_number(inv: Inv, rng: random.Random) -> None:
    inv.number = None


# plant name -> (mutation, check codes it must trigger)
PLANTS: dict[str, tuple[Callable[[Inv, random.Random], None], frozenset[str]]] = {
    "line_math": (plant_line_math, frozenset({"line_math"})),
    "subtotal": (plant_subtotal, frozenset({"subtotal_mismatch"})),
    "total": (plant_total, frozenset({"total_mismatch"})),
    "tax_twice": (plant_tax_twice, frozenset({"total_mismatch"})),
    # one wrong row breaks its own math AND bases + tax no longer reach the total
    "tax_row": (plant_tax_row, frozenset({"tax_line_math", "tax_base_mismatch"})),
    "due_before": (plant_due_before, frozenset({"due_before_invoice"})),
    "old_date": (plant_old_date, frozenset({"date_implausible"})),
    "no_number": (plant_no_number, frozenset({"missing_field"})),
}


# --- formatting ------------------------------------------------------------------

SEPARATORS = {"en": (",", "."), "in": (",", "."), "eu": (".", ","), "eu_space": (" ", ",")}


def fmt_num(d: Decimal, style: str, places: int = 2) -> str:
    sign = "-" if d < 0 else ""
    text = f"{abs(d):.{places}f}" if places else f"{abs(d):f}"
    whole, _, frac = text.partition(".")
    thou, dec = SEPARATORS[style]
    if style == "in" and len(whole) > 3:  # Indian grouping: 12,34,567
        head, tail = whole[:-3], whole[-3:]
        groups = [head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1]
        whole = ",".join(groups + [tail])
    else:
        whole = f"{int(whole):,}".replace(",", thou)
    return sign + whole + (dec + frac if frac else "")


def fmt_qty(q: Decimal, style: str) -> str:
    q = q.normalize()
    return fmt_num(q, style, places=0 if q == q.to_integral() else 1)


def fmt_money(d: Decimal, v: Vendor) -> str:
    """'£1,234.56', '1.234,56 €', 'Rs. 1,23,456.00', 'kr 1 234,56', '-$120.00'."""
    n, sign = fmt_num(abs(d), v.number_style), "-" if d < 0 else ""
    if v.symbol_after:
        return f"{sign}{n} {v.symbol}"
    gap = " " if v.symbol[-1].isalpha() or v.symbol.endswith(".") else ""
    return f"{sign}{v.symbol}{gap}{n}"


def fmt_date(d: date, style: str) -> str:
    return {
        "dmy": f"{d.day:02d}/{d.month:02d}/{d.year}",
        "mdy": f"{d.month:02d}/{d.day:02d}/{d.year}",
        "dmy_dot": f"{d.day:02d}.{d.month:02d}.{d.year}",
        "iso": d.isoformat(),
        "long": f"{MONTHS[d.month - 1]} {d.day}, {d.year}",
    }[style]


# --- layouts -----------------------------------------------------------------------


class Layout:
    """Shared table + pagination. Subclasses draw the header/totals differently."""

    bottom = 140  # leave room for totals / footer
    cols = (50, 360, 450, 545)  # description, qty (right), unit (right), amount (right)

    def __init__(self, c: Canvas, inv: Inv) -> None:
        self.c, self.inv, self.v = c, inv, inv.vendor
        self.L = LABELS[self.v.lang]

    def render(self) -> None:
        y = self.header()
        y = self.table(y)
        y = self.totals(y)
        if self.inv.multi_rate:
            self.tax_table(y - 14)
        self.footer()

    def tax_table(self, y: float) -> None:
        """The till-receipt tax summary: one row per rate with base and tax."""
        c, L, s = self.c, self.L, self.v.number_style
        c.setFont("Helvetica-Bold", 9)
        c.drawString(300, y, L["rate"])
        c.drawRightString(450, y, L["base"])
        c.drawRightString(545, y, L["tax_amt"])
        c.setFont("Helvetica", 9)
        for row in self.inv.tax_rows or []:
            y -= 14
            c.drawString(300, y, f"{self.v.tax_name} {fmt_num(pct(row.rate), s, places=0)}%")
            c.drawRightString(450, y, fmt_num(row.base, s) if row.base is not None else "")
            c.drawRightString(545, y, fmt_num(row.amount, s))

    def title(self) -> str:
        return self.L["credit"] if self.inv.credit_note else self.L["invoice"]

    def table_header(self, y: float) -> float:
        c, (x0, x1, x2, x3) = self.c, self.cols
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x0, y, self.L["desc"])
        c.drawRightString(x1, y, self.L["qty"])
        c.drawRightString(x2, y, self.L["unit"])
        c.drawRightString(x3, y, self.L["amount"])
        c.line(x0, y - 5, x3, y - 5)
        return y - 20

    def row(self, y: float, i: int, line: Line) -> None:
        c, (x0, x1, x2, x3), s = self.c, self.cols, self.v.number_style
        c.setFont("Helvetica", 9)
        c.drawString(x0, y, line.description)
        c.drawRightString(x1, y, fmt_qty(line.quantity, s))
        c.drawRightString(x2, y, fmt_num(line.unit_price, s))
        c.drawRightString(x3, y, fmt_num(line.amount, s))

    def table(self, y: float) -> float:
        y = self.table_header(y)
        for i, line in enumerate(self.inv.lines):
            if y < self.bottom:
                self.footer()
                self.c.showPage()
                self.c.setFont("Helvetica", 9)
                self.c.drawString(50, H - 50, f"{self.v.name} - {self.title()} {self.inv.number or ''} {self.L['cont']}")
                y = self.table_header(H - 80)
            self.row(y, i, line)
            y -= 16
        return y - 10

    def total_rows(self) -> list[tuple[str, str, bool]]:
        inv, v, s = self.inv, self.v, self.v.number_style
        rows = []
        if inv.subtotal is not None:
            rows.append((self.L["subtotal"], fmt_num(inv.subtotal, s), False))
        if inv.discount is not None:
            rows.append((f"{self.L['discount']} ({inv.discount_pct}%)", fmt_num(inv.discount, s), False))
        if inv.tax is not None and not inv.tax_inclusive:
            rows.append((v.tax_name or "Tax", fmt_num(inv.tax, s), False))
        rows.append((f"{self.L['total']} ({v.currency})", fmt_money(inv.total, v), True))
        if inv.tax is not None and inv.tax_inclusive:
            rows.append((self.L["incl"].format(v.tax_name), fmt_num(inv.tax, s), False))
        return rows

    def totals(self, y: float) -> float:
        c = self.c
        for label, value, bold in self.total_rows():
            c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
            c.drawRightString(450, y, label)
            c.drawRightString(545, y, value)
            y -= 16
        return y

    def footer(self) -> None:
        pass

    def meta(self) -> list[tuple[str, str]]:
        inv, L, ds = self.inv, self.L, self.v.date_style
        rows = []
        if inv.number:
            rows.append((L["number"], inv.number))
        rows.append((L["date"], fmt_date(inv.issued, ds)))
        if inv.due:
            rows.append((L["due"], fmt_date(inv.due, ds)))
        return rows

    def vendor_lines(self) -> list[str]:
        v = self.v
        return [*v.address, *([f"{v.tax_label}: {v.tax_id}"] if v.tax_id else [])]


class Classic(Layout):
    """Vendor top-left, title + meta top-right, ruled table."""

    def header(self) -> float:
        c, y = self.c, H - 60
        c.setFont("Helvetica-Bold", 18)
        c.drawString(50, y, self.v.name)
        c.setFont("Helvetica", 10)
        for k, text in enumerate(self.vendor_lines()):
            c.drawString(50, y - 16 - 14 * k, text)
        c.setFont("Helvetica-Bold", 14)
        c.drawRightString(W - 50, y, self.title())
        c.setFont("Helvetica", 10)
        for k, (label, value) in enumerate(self.meta()):
            c.drawRightString(W - 50, y - 16 - 14 * k, f"{label}: {value}")
        y -= 100
        if self.inv.bill_to:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(50, y, f"{self.L['bill_to']}:")
            c.setFont("Helvetica", 10)
            c.drawString(50, y - 14, self.inv.bill_to)
        return y - 50


class Band(Layout):
    """Colored header band, boxed meta grid, shaded rows, totals in a box."""

    def header(self) -> float:
        c = self.c
        c.setFillColorRGB(0.12, 0.23, 0.42)
        c.rect(0, H - 110, W, 110, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(50, H - 45, self.v.name)
        c.setFont("Helvetica", 9)
        for k, text in enumerate(self.vendor_lines()):
            c.drawString(50, H - 62 - 12 * k, text)
        c.setFont("Helvetica-Bold", 24)
        c.drawRightString(W - 50, H - 50, self.title())
        c.setFillColorRGB(0, 0, 0)

        y = H - 150
        for k, (label, value) in enumerate(self.meta()):
            x = 50 + 135 * k
            c.setStrokeColorRGB(0.7, 0.7, 0.7)
            c.rect(x, y - 12, 125, 34, stroke=1, fill=0)
            c.setFont("Helvetica", 7)
            c.setFillColorRGB(0.4, 0.4, 0.4)
            c.drawString(x + 6, y + 12, label.upper())
            c.setFillColorRGB(0, 0, 0)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(x + 6, y - 4, value)
        if self.inv.bill_to:
            c.setFont("Helvetica", 7)
            c.setFillColorRGB(0.4, 0.4, 0.4)
            c.drawString(50, y - 35, self.L["bill_to"].upper())
            c.setFillColorRGB(0, 0, 0)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(50, y - 49, self.inv.bill_to)
        c.setStrokeColorRGB(0, 0, 0)
        return y - 90

    def row(self, y: float, i: int, line: Line) -> None:
        if i % 2:
            self.c.setFillColorRGB(0.94, 0.95, 0.97)
            self.c.rect(45, y - 4, W - 95, 15, stroke=0, fill=1)
            self.c.setFillColorRGB(0, 0, 0)
        super().row(y, i, line)

    def totals(self, y: float) -> float:
        rows = self.total_rows()
        self.c.setStrokeColorRGB(0.12, 0.23, 0.42)
        self.c.rect(300, y - 16 * len(rows) + 6, 250, 16 * len(rows) + 6, stroke=1, fill=0)
        return super().totals(y)


class Minimal(Layout):
    """Big title, meta on one line, no rules, vendor details only in the FOOTER."""

    bottom = 170

    def header(self) -> float:
        c, inv = self.c, self.inv
        c.setFont("Helvetica-Bold", 30)
        c.drawString(50, H - 80, self.title().capitalize())
        c.setFont("Helvetica", 10)
        c.drawString(50, H - 105, "   ·   ".join(f"{label} {value}" for label, value in self.meta()))
        if inv.bill_to:
            c.drawString(50, H - 125, f"{self.L['bill_to']}: {inv.bill_to}")
        return H - 175

    def table_header(self, y: float) -> float:
        c, (x0, x1, x2, x3) = self.c, self.cols
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(x0, y, self.L["desc"].upper())
        c.drawRightString(x1, y, self.L["qty"].upper())
        c.drawRightString(x2, y, self.L["unit"].upper())
        c.drawRightString(x3, y, self.L["amount"].upper())
        c.setFillColorRGB(0, 0, 0)
        return y - 18

    def footer(self) -> None:
        c, v = self.c, self.v
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.line(50, 75, W - 50, 75)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(50, 60, v.name)
        c.setFont("Helvetica", 8)
        c.drawString(50, 48, " · ".join(self.vendor_lines()))


LAYOUTS = {"classic": Classic, "band": Band, "minimal": Minimal}


# --- output -----------------------------------------------------------------------


def render_pdf(inv: Inv, layout: str) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=A4, invariant=1)  # invariant: no timestamps -> same bytes each run
    c.setTitle(f"{inv.vendor.name} {inv.number or ''}")
    LAYOUTS[layout](c, inv).render()
    c.save()
    return buf.getvalue()


@dataclass(frozen=True)
class Spec:
    id: str
    vendor: str
    layout: str
    n_lines: int
    fmt: str = "pdf"  # pdf | png | scan (jpg)
    plant: tuple[str, ...] = ()
    discount_pct: int | None = None
    negative_discount: bool = False
    credit_note: bool = False
    tax_inclusive: bool = False
    no_due: bool = False
    no_bill_to: bool = False
    duplicate_of: str | None = None
    note: str = ""
