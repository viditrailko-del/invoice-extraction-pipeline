"""What one LLM call cost, in USD.

Prices are per 1M tokens, standard (non-batch) tier, from
https://ai.google.dev/gemini-api/docs/pricing (checked 2026-11-11).
Gemini bills thinking tokens as output, so they are added to the output count.

Update this table when prices change. An unknown model gives cost None (not 0):
"we don't know" must never look like "it was free".
"""

from decimal import Decimal

PER_MILLION = Decimal(1_000_000)

# model -> (input USD per 1M tokens, output USD per 1M tokens)
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    # gemini-3.5-flash is deprecated and Google now routes it to gemini-3.6-flash,
    # so it is billed at the 3.6 rate.
    "gemini-3.5-flash": (Decimal("1.50"), Decimal("7.50")),
    "gemini-3.6-flash": (Decimal("1.50"), Decimal("7.50")),
    "gemini-3.5-flash-lite": (Decimal("0.30"), Decimal("2.50")),
}


def cost_usd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    thinking_tokens: int | None = None,
) -> Decimal | None:
    prices = PRICES.get(model)
    if prices is None or input_tokens is None or output_tokens is None:
        return None
    in_price, out_price = prices
    billed_output = output_tokens + (thinking_tokens or 0)
    cost = (input_tokens * in_price + billed_output * out_price) / PER_MILLION
    return cost.quantize(Decimal("0.000001"))  # matches the Numeric(10, 6) column
