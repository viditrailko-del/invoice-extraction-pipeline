"""Live mode for the public demo: a visitor's own file (upload or generated) read by real Gemini.

Everything after admission is the normal pipeline (process_invoice); this module only decides which model answers
and whether there is quota left.

Quota: live extractions spend the free tier, so there is a daily cap for the whole demo and an hourly cap per
visitor IP. The counter lives in memory (the demo runs as one process) and is re-read from the DB at the first
request of each UTC day, so a restart doesn't hand out a fresh day. The per-IP cap trusts the proxy's
X-Forwarded-For (uvicorn --proxy-headers); the daily cap is what protects the quota.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import distinct, func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ExtractionRun, Invoice, InvoiceStatus
from app.demo.replay import REPLAY_PREFIX


class QuotaExceeded(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LiveQuota:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: date | None = None
        self._used = 0
        self._by_ip: dict[str, deque[datetime]] = {}

    def reset(self) -> None:
        with self._lock:
            self._day, self._used, self._by_ip = None, 0, {}

    def used_today(self, db: Session, now: datetime) -> int:
        with self._lock:
            self._roll(db, now)
            return self._used

    def take(self, db: Session, ip: str, settings: Settings, now: datetime) -> None:
        """Reserve one live extraction for this IP, or raise QuotaExceeded."""
        with self._lock:
            self._roll(db, now)
            if self._used >= settings.demo_live_daily_limit:
                raise QuotaExceeded("Today's live extractions are used up (the demo runs on Gemini's free tier). "
                                    "The recorded invoices still work; live mode is back tomorrow (UTC).")
            recent = self._by_ip.setdefault(ip, deque())
            while recent and now - recent[0] >= timedelta(hours=1):
                recent.popleft()
            if len(recent) >= settings.demo_live_per_ip_per_hour:
                raise QuotaExceeded("You've used the live extractions allowed per hour. Try the recorded "
                                    "invoices, or come back in a little while.")
            recent.append(now)
            self._used += 1

    def _roll(self, db: Session, now: datetime) -> None:
        if self._day != now.date():
            self._day, self._by_ip = now.date(), {}
            self._used = count_live_today(db, now)


def count_live_today(db: Session, now: datetime) -> int:
    """Invoices that called a real model (not a replay) today (UTC)."""
    start = datetime.combine(now.date(), time(), UTC)
    return db.scalar(
        select(func.count(distinct(ExtractionRun.invoice_id)))
        .where(ExtractionRun.created_at >= start, ExtractionRun.model.not_like(f"{REPLAY_PREFIX}%"))
    ) or 0


quota = LiveQuota()


def live_settings(settings: Settings) -> Settings:
    """Live extractions: flash-lite only (the main model's ~20/day can't be shared between demos)."""
    return settings.model_copy(update={"gemini_model": settings.demo_live_model, "gemini_fallback_model": ""})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def admit_live(db: Session, request: Request, settings: Settings) -> Settings:
    """Take one live slot (or 429) and return the settings a live extraction runs with. Used by every route that
    calls Gemini in demo mode (upload, retry, generate), so /docs can't get around the cap."""
    try:
        quota.take(db, client_ip(request), settings, datetime.now(UTC))
    except QuotaExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.message) from None
    return live_settings(settings)


def fail_interrupted_jobs(session_factory: Callable[[], Session]) -> int:
    """Demo-mode startup: invoices a restart left `pending`/`processing` end `extraction_failed` instead of being
    re-run. Re-running would spend live quota nobody was admitted for (or call Gemini for a replay); the visitor
    can press retry, which goes through admission like any live call. (A free instance restarts when it sleeps.)"""
    with session_factory() as db:
        ids = db.scalars(
            update(Invoice)
            .where(Invoice.status.in_([InvoiceStatus.pending, InvoiceStatus.processing]))
            .values(status=InvoiceStatus.extraction_failed, processing_started_at=None)
            .returning(Invoice.id)
        ).all()
        db.commit()
    return len(ids)
