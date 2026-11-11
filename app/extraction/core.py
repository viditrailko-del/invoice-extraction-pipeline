"""The LLM part of extraction, with no database: shared by the API and the eval harness.

extract_invoice() owns all the "the model may be wrong, slow, or down" logic:
  - transient errors (overload, timeout): retry with backoff, then the fallback model
  - output that doesn't fit the schema: retry ONCE, telling the model what was wrong

It never writes anywhere itself. Every LLM call is reported to a `RunRecorder`:
the API's recorder saves it as an extraction_runs row, the eval's keeps it in memory
and writes it to a results file. That way the eval measures exactly the code the API
runs, instead of a copy that could drift apart from it.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from app.config import Settings
from app.extraction.client import Extractor, LLMError
from app.extraction.pricing import cost_usd
from app.logging_setup import log_event
from app.schemas.extraction import ExtractedInvoice

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_TRANSIENT_RETRIES = 2  # per model, so up to 3 calls each
MAX_VALIDATION_RETRIES = 1

RETRY_FEEDBACK = """

Your previous answer was rejected because it did not match the required schema.
Errors:
{errors}

Return the corrected JSON for the same invoice. Follow the schema exactly."""


@dataclass
class Attempt:
    """One LLM call and what came of it (one extraction_runs row in the API)."""

    model: str
    prompt_version: str
    raw_response: str | None = None
    error: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cost_usd: Decimal | None = None


class RunRecorder(Protocol):
    def record(self, attempt: Attempt) -> None:
        """Called right after each LLM call, BEFORE its output is parsed."""

    def update(self, attempt: Attempt) -> None:
        """Called when a recorded attempt changes (its output failed validation)."""


@lru_cache
def load_prompt(version: str) -> str:
    return (PROMPTS_DIR / f"{version}.txt").read_text(encoding="utf-8")


@lru_cache
def response_schema() -> dict:
    return ExtractedInvoice.model_json_schema()


def extract_invoice(
    file_bytes: bytes,
    mime_type: str,
    extractor: Extractor,
    settings: Settings,
    recorder: RunRecorder,
    sleep: Callable[[float], None] = time.sleep,
) -> ExtractedInvoice | None:
    """Returns the validated extraction, or None if every attempt failed."""
    base_prompt = load_prompt(settings.prompt_version)
    prompt = base_prompt

    for _ in range(1 + MAX_VALIDATION_RETRIES):
        attempt = _call_llm(file_bytes, mime_type, prompt, extractor, settings, recorder, sleep)
        if attempt is None:
            return None  # every model/attempt failed at the API level
        try:
            return ExtractedInvoice.model_validate_json(attempt.raw_response or "")
        except ValidationError as e:
            attempt.error = f"validation: {e}"[:4000]
            recorder.update(attempt)
            log_event(log, logging.WARNING, "llm_output_invalid", model=attempt.model,
                      error_count=e.error_count(), first_error=_short_errors(e).splitlines()[0][:200])
            prompt = base_prompt + RETRY_FEEDBACK.format(errors=_short_errors(e))
    return None


def _call_llm(
    file_bytes: bytes,
    mime_type: str,
    prompt: str,
    extractor: Extractor,
    settings: Settings,
    recorder: RunRecorder,
    sleep: Callable[[float], None],
) -> Attempt | None:
    models = [m for m in (settings.gemini_model, settings.gemini_fallback_model) if m]
    for model in models:
        for attempt_no in range(1 + MAX_TRANSIENT_RETRIES):
            attempt = Attempt(model=model, prompt_version=settings.prompt_version)
            try:
                resp = extractor.generate(
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                    prompt=prompt,
                    schema=response_schema(),
                    model=model,
                )
            except LLMError as e:
                attempt.error = str(e)[:4000]
                attempt.latency_ms = e.latency_ms
                recorder.record(attempt)
                log_event(log, logging.WARNING, "llm_call_failed", model=model,
                          transient=e.transient, latency_ms=e.latency_ms, error=str(e)[:200])
                if not e.transient:
                    break  # e.g. bad request: same model won't fix it, try the fallback
                if attempt_no < MAX_TRANSIENT_RETRIES:
                    sleep(settings.llm_retry_backoff_s * (2**attempt_no))
                continue

            attempt.raw_response = resp.text
            attempt.latency_ms = resp.latency_ms
            attempt.input_tokens = resp.input_tokens
            attempt.output_tokens = resp.output_tokens
            attempt.thinking_tokens = resp.thinking_tokens
            attempt.cost_usd = cost_usd(
                model, resp.input_tokens, resp.output_tokens, resp.thinking_tokens
            )
            recorder.record(attempt)  # raw output is saved before we even try to parse it
            return attempt
    return None


def _short_errors(e: ValidationError) -> str:
    return "\n".join(
        f"- {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in e.errors()[:20]
    )
