"""Replay mode for the public demo: the real pipeline, with Gemini's answers pre-recorded.

A scenario (app/demo/scenarios/<name>.json, built by `python -m demo.build_replay`) holds one synthetic invoice
file (scenarios/files/) and every call flash-lite made for it in a frozen eval run, errors included. Replaying it
runs exactly what an upload runs (process_invoice: retries, schema validation, the CURRENT checks, duplicate
detection, the stored extraction_runs and field_flags); only Gemini is replaced:

- `RecordedExtractor` returns the recorded calls in order and refuses to improvise. It answers only for the
  recorded file bytes (sha256), the recorded prompt file (prompt version + text hash) and the response schema it
  was recorded with. Any difference raises a non-transient LLMError, so the invoice ends `extraction_failed`
  with "replay diverged" instead of showing an answer to a question the model was never asked.

Replayed calls are stored under the model name `replay:<recorded model>`: no price is known for that name, so
their cost is None, and the live-mode quota (counted on real model names) never counts them.

The date checks use the day the recording was scored (`as_of`, stored as invoices.checks_as_of), so a replay
gives the same flags next year. A replay first deletes the visitor's earlier copy of the same file (the
idempotency key is per sandbox), and never touches another sandbox.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Invoice, InvoiceStatus
from app.demo.cleanup import delete_invoices
from app.extraction.client import LLMError, LLMResponse
from app.extraction.service import process_invoice

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
FILES_DIR = SCENARIOS_DIR / "files"
REPLAY_PREFIX = "replay:"
SUMMARY_KEYS = ("name", "group", "title", "blurb", "fixture", "file", "content_type", "depends_on")


@dataclass(frozen=True)
class Scenario:
    name: str
    data: dict[str, Any]

    @property
    def path(self) -> Path:
        return FILES_DIR / self.data["file"]

    @property
    def as_of(self) -> date:
        return date.fromisoformat(self.data["as_of"])

    @property
    def depends_on(self) -> list[str]:
        return self.data.get("depends_on", [])

    def summary(self) -> dict[str, Any]:
        """What the demo page lists: no recorded outputs, no expected result."""
        return {k: self.data.get(k) for k in SUMMARY_KEYS}


@lru_cache
def scenarios() -> dict[str, Scenario]:
    """name -> scenario, in the order the demo page lists them (the builder's `order`)."""
    loaded = [json.loads(p.read_text(encoding="utf-8")) for p in SCENARIOS_DIR.glob("*.json")]
    return {d["name"]: Scenario(name=d["name"], data=d) for d in sorted(loaded, key=lambda d: d["order"])}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def schema_sha(schema: dict[str, Any]) -> str:
    """Same fingerprint as app.extraction.core.schema_fingerprint, for the schema actually sent."""
    return sha256(json.dumps(schema, sort_keys=True).encode())[:12]


class RecordedExtractor:
    """An Extractor that replays a scenario's recorded calls, in order, and only for the recorded input."""

    def __init__(self, recorded: dict[str, Any]) -> None:
        self._rec = recorded
        self._calls: Iterator[dict[str, Any]] = iter(recorded["calls"])

    def generate(self, *, file_bytes: bytes, mime_type: str, prompt: str, schema: dict[str, Any],
                 model: str) -> LLMResponse:
        rec = self._rec
        if sha256(file_bytes) != rec["file_sha256"]:
            raise LLMError("replay diverged: the file differs from the recording", transient=False)
        # A validation retry sends the same prompt plus feedback, so the prompt must START with the recorded one.
        if sha256(prompt[: rec["prompt_chars"]].encode()) != rec["prompt_sha256"]:
            raise LLMError("replay diverged: the prompt differs from the recording", transient=False)
        if schema_sha(schema) != rec["schema_sha"]:
            raise LLMError("replay diverged: the response schema differs from the recording", transient=False)
        call = next(self._calls, None)
        if call is None:
            raise LLMError("replay: no recorded answer left", transient=False)
        if call["error"] is not None and call["raw_response"] is None:  # the API call itself failed
            raise LLMError(call["error"], transient=call["transient"], latency_ms=call["latency_ms"] or 0)
        return LLMResponse(text=call["raw_response"], model=model, latency_ms=call["latency_ms"] or 0,
                           input_tokens=call["input_tokens"], output_tokens=call["output_tokens"],
                           thinking_tokens=call["thinking_tokens"])


def replay_settings(settings: Settings, recorded: dict[str, Any]) -> Settings:
    """The app's settings with the recorded model and prompt version: calls are labeled `replay:<model>`, there
    is no fallback model, and no retry waits."""
    return settings.model_copy(update={
        "gemini_model": REPLAY_PREFIX + recorded["model"], "gemini_fallback_model": "",
        "prompt_version": recorded["prompt_version"], "llm_retry_backoff_s": 0,
    })


# One replay per sandbox at a time (the demo runs as one process): a replay deletes the sandbox's earlier copy,
# which must not happen under one that is still running.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(sandbox: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(sandbox, threading.Lock())


def replay(db: Session, session_factory: Callable[[], Session], scenario: Scenario, sandbox: str,
           settings: Settings) -> uuid.UUID:
    """Replay a scenario in the visitor's sandbox (its dependencies first, if missing). Returns the invoice id."""
    with _lock(sandbox):
        for name in scenario.depends_on:
            dep = scenarios()[name]
            if _find(db, dep, sandbox) is None:
                _run(db, session_factory, dep, sandbox, settings)
        return _run(db, session_factory, scenario, sandbox, settings)


def _find(db: Session, scenario: Scenario, sandbox: str) -> Invoice | None:
    return db.scalar(select(Invoice).where(
        Invoice.demo_session == sandbox, Invoice.file_hash == scenario.data["recorded"]["file_sha256"],
        Invoice.status.not_in([InvoiceStatus.pending, InvoiceStatus.processing]),
    ))


def _run(db: Session, session_factory: Callable[[], Session], scenario: Scenario, sandbox: str,
         settings: Settings) -> uuid.UUID:
    file_hash = sha256(scenario.path.read_bytes())
    earlier = db.scalars(select(Invoice.id).where(Invoice.demo_session == sandbox,
                                                  Invoice.file_hash == file_hash)).all()
    delete_invoices(db, list(earlier))
    invoice = Invoice(
        file_hash=file_hash, original_filename=scenario.data["file"], content_type=scenario.data["content_type"],
        file_path=str(scenario.path), status=InvoiceStatus.pending, demo_session=sandbox,
        checks_as_of=scenario.as_of,
    )
    db.add(invoice)
    db.commit()
    rec = scenario.data["recorded"]
    # Synchronous (no Gemini, ~10 ms of checks): the visitor gets the final result in the response.
    process_invoice(invoice.id, session_factory, RecordedExtractor(rec), replay_settings(settings, rec))
    return invoice.id
