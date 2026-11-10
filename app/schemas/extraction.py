"""The shape of data we want out of an invoice. SINGLE SOURCE OF TRUTH.

This same model is:
  1. sent to Gemini as the response JSON schema (so it answers in this shape), and
  2. used to validate whatever Gemini actually returns (never trust it blindly).

Every field is optional on purpose: if a value isn't on the invoice, the model must
return null rather than invent one. A missing value is honest; a guessed one is a bug.
Money is Decimal (exact), never float.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Money = Annotated[Decimal, Field(max_digits=14, decimal_places=4)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class Vendor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, description="Seller / supplier company name")
    address: str | None = Field(None, description="Seller address as one line")
    tax_id: str | None = Field(None, description="Seller VAT/GST/tax ID exactly as printed")


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Money | None = None
    amount: Money | None = Field(None, description="Line total exactly as printed")


class ExtractedInvoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None = None
    invoice_date: date | None = Field(None, description="ISO format YYYY-MM-DD")
    due_date: date | None = Field(None, description="ISO format YYYY-MM-DD")
    vendor: Vendor = Field(default_factory=Vendor)
    bill_to_name: str | None = Field(None, description="Buyer / customer name")
    currency: CurrencyCode | None = Field(None, description="ISO 4217 code, e.g. USD, EUR, INR")
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Money | None = None
    tax: Money | None = Field(None, description="Total tax amount, not the rate")
    discount: Money | None = None
    total: Money | None = Field(None, description="Final amount due")
    # Varies by document type, not just country (US: tax added on top; EU shop
    # receipts: tax already inside the prices). Read from the document, never assumed:
    # the total check is strict when this is known and lenient only when it's null.
    prices_include_tax: bool | None = Field(
        None,
        description=(
            "true if the document says tax is INCLUDED in the prices/total "
            "(e.g. 'VAT included', 'IVA incluído', 'inkl. MwSt.', 'dont TVA'); "
            "false if tax is shown as a separate amount ADDED to a subtotal/net amount "
            "to reach the total; null if no tax is shown or it is unclear"
        ),
    )
