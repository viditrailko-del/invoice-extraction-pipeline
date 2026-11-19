"""Visitor sandboxes for the public demo.

In demo mode every visitor works in their own sandbox: a random id their browser makes once and sends as the
`X-Demo-Session` header. Each invoice stores it (`invoices.demo_session`), and every lookup is limited to it, so
visitors never see each other's uploads, a replay never deletes someone else's copy, and duplicate detection
only compares invoices within one sandbox.

It is a partition, not a security boundary: anyone who learns a sandbox id can use it. Outside demo mode there
is no sandbox (None) and nothing is filtered.
"""

import re
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import Select

from app.config import Settings, get_settings
from app.db.models import Invoice

SANDBOX_HEADER = "X-Demo-Session"
_VALID = re.compile(r"[A-Za-z0-9_-]{16,64}")


def get_sandbox(
    settings: Annotated[Settings, Depends(get_settings)],
    x_demo_session: Annotated[str | None, Header()] = None,
) -> str | None:
    if not settings.demo_mode:
        return None
    if x_demo_session is None or not _VALID.fullmatch(x_demo_session):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Demo mode: send a {SANDBOX_HEADER} header (16-64 letters, digits, '-' or '_'), "
            "any random id you keep for your session.",
        )
    return x_demo_session


Sandbox = Annotated[str | None, Depends(get_sandbox)]


def in_sandbox(stmt: Select, sandbox: str | None) -> Select:
    """Limit an invoice query to the visitor's sandbox (no-op outside demo mode)."""
    return stmt if sandbox is None else stmt.where(Invoice.demo_session == sandbox)
