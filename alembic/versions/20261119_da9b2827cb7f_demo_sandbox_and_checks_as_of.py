"""demo sandbox and checks as_of

Public demo (Phase 7): invoices.demo_session (a visitor's sandbox) and invoices.checks_as_of (the
"today" the date checks use for a replayed recording). The idempotency key becomes (file_hash,
demo_session) NULLS NOT DISTINCT, which is exactly the old unique file_hash for non-demo rows (all NULL).

Revision ID: da9b2827cb7f
Revises: 507161ba878a
Create Date: 2026-11-19 07:04:03.935729

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'da9b2827cb7f'
down_revision: Union[str, None] = '507161ba878a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("demo_session", sa.String(length=64), nullable=True))
    op.add_column("invoices", sa.Column("checks_as_of", sa.Date(), nullable=True))
    op.create_index(op.f("ix_invoices_demo_session"), "invoices", ["demo_session"], unique=False)
    # New key first, then the old unique index becomes a plain one (all in one transaction).
    op.create_unique_constraint(
        "uq_invoices_file_hash_demo_session", "invoices", ["file_hash", "demo_session"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_index(op.f("ix_invoices_file_hash"), table_name="invoices")
    op.create_index(op.f("ix_invoices_file_hash"), "invoices", ["file_hash"], unique=False)


def downgrade() -> None:
    # Fails if two sandboxes hold the same file: delete demo rows first.
    op.drop_index(op.f("ix_invoices_file_hash"), table_name="invoices")
    op.create_index(op.f("ix_invoices_file_hash"), "invoices", ["file_hash"], unique=True)
    op.drop_constraint("uq_invoices_file_hash_demo_session", "invoices", type_="unique")
    op.drop_index(op.f("ix_invoices_demo_session"), table_name="invoices")
    op.drop_column("invoices", "checks_as_of")
    op.drop_column("invoices", "demo_session")
