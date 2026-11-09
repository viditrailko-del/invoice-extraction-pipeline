"""Generate a synthetic invoice PDF for manual/smoke testing.

Usage (inside the container):
    uv run python scripts/make_sample_invoice.py storage/samples/sample_invoice.pdf
"""

import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ITEMS = [
    ("Cloud hosting - October", 1, "450.00"),
    ("Support hours", 12, "85.00"),
    ("SSL certificate", 2, "39.50"),
]


def make(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    y = h - 60
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, y, "Northwind Cloud Services Ltd.")
    c.setFont("Helvetica", 10)
    c.drawString(50, y - 16, "12 Harbour Street, Bristol BS1 4XY, United Kingdom")
    c.drawString(50, y - 30, "VAT No: GB123456789")
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(w - 50, y, "INVOICE")
    c.setFont("Helvetica", 10)
    c.drawRightString(w - 50, y - 16, "Invoice #: NW-2026-0417")
    c.drawRightString(w - 50, y - 30, "Date: 03/11/2026")
    c.drawRightString(w - 50, y - 44, "Due: 03/12/2026")

    y -= 90
    c.drawString(50, y, "Bill to: Acme Analytics Pvt Ltd")

    y -= 40
    c.setFont("Helvetica-Bold", 10)
    for x, label in [(50, "Description"), (320, "Qty"), (390, "Unit price"), (490, "Amount")]:
        c.drawString(x, y, label)
    c.setFont("Helvetica", 10)
    subtotal = 0
    for desc, qty, price in ITEMS:
        y -= 18
        amount = qty * float(price)
        subtotal += amount
        c.drawString(50, y, desc)
        c.drawString(320, y, str(qty))
        c.drawString(390, y, f"£{price}")
        c.drawString(490, y, f"£{amount:,.2f}")

    tax = round(subtotal * 0.20, 2)
    y -= 40
    for label, val in [("Subtotal", subtotal), ("VAT (20%)", tax), ("Total due", subtotal + tax)]:
        c.drawString(390, y, label)
        c.drawString(490, y, f"£{val:,.2f}")
        y -= 16
    c.save()


if __name__ == "__main__":
    make(Path(sys.argv[1] if len(sys.argv) > 1 else "storage/samples/sample_invoice.pdf"))
