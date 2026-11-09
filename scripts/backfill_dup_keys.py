"""One-off backfill: compute dup_key for invoices extracted before it existed,
then flag any business duplicates among them.

Safe to run any number of times (idempotent): existing keys and flags are kept,
nothing is flagged twice.

    docker compose exec -T api uv run --frozen python -m scripts.backfill_dup_keys
"""

from collections import defaultdict

from sqlalchemy import select

from app.checks.duplicates import make_dup_key
from app.db.models import FieldFlag, Invoice, Severity
from app.db.session import SessionLocal
from app.schemas.extraction import ExtractedInvoice


def main() -> None:
    with SessionLocal() as db:
        invoices = db.scalars(
            select(Invoice).where(Invoice.extracted_json.is_not(None)).order_by(Invoice.created_at)
        ).all()

        keyed = 0
        for inv in invoices:
            if inv.dup_key is None:
                inv.dup_key = make_dup_key(ExtractedInvoice.model_validate(inv.extracted_json))
                keyed += inv.dup_key is not None

        # Each invoice is flagged against every EARLIER invoice with the same key,
        # the same thing that happens live when a new upload arrives.
        groups: dict[str, list[Invoice]] = defaultdict(list)
        for inv in invoices:
            if inv.dup_key:
                groups[inv.dup_key].append(inv)

        flagged = 0
        for group in groups.values():
            for i, inv in enumerate(group):
                existing = {f.reason for f in inv.flags}
                for earlier in group[:i]:
                    if any(str(earlier.id) in r for r in existing):
                        continue
                    db.add(
                        FieldFlag(
                            invoice_id=inv.id,
                            field_path="invoice_number",
                            severity=Severity.error,
                            reason=(
                                f"Possible duplicate of invoice {earlier.id} "
                                f"(same vendor and invoice number; totals: "
                                f"{inv.extracted_json.get('total')} vs "
                                f"{earlier.extracted_json.get('total')})"
                            ),
                        )
                    )
                    flagged += 1
        db.commit()
    print(f"checked {len(invoices)} invoices: {keyed} keys added, {flagged} duplicate flags added")


if __name__ == "__main__":
    main()
