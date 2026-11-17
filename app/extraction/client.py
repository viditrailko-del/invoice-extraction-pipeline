"""Talking to the LLM. This file knows about Gemini; nothing else in the app does.

The rest of the app depends only on the `Extractor` protocol, so tests can plug in a
fake extractor and never call the real API (CLAUDE.md rule: tests never hit Gemini).
"""

import io
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from google import genai
from google.genai import errors, types

from app.logging_setup import log_event

try:  # google-genai 3.x sends requests through httpx2, a separate package: its errors are NOT httpx errors.
    import httpx2
except ImportError:  # pragma: no cover - older SDKs use plain httpx
    httpx2 = httpx

log = logging.getLogger(__name__)

# Network-level failures from either HTTP library. Catching only httpx's let a "Server
# disconnected" (httpx2.RemoteProtocolError) escape as a raw exception: a 500 instead of
# extraction_failed (rule 8). Found in the sibling support_agent_project's eval run.
TIMEOUT_ERRORS = (httpx.TimeoutException, httpx2.TimeoutException)
TRANSPORT_ERRORS = (httpx.TransportError, httpx2.TransportError)

# Gemini rejects inline requests above ~20 MB; base64 adds ~33%, so upload anything
# bigger than this through the Files API instead of sending the bytes inline.
INLINE_LIMIT_BYTES = 14 * 1024 * 1024

# HTTP codes worth retrying: rate limit, server errors, overloaded.
TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}

# A 429 means either "too many per MINUTE" (wait a few seconds and retry) or "too many
# per DAY" (this model is done until the quota resets). Retrying a daily quota only
# burns time (~15 s per invoice in the first eval run), so it goes straight to the
# fallback model instead.
DAILY_QUOTA = re.compile(r"per day|PerDay", re.IGNORECASE)


@dataclass
class LLMResponse:
    text: str | None
    model: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None  # billed as output, but not in output_tokens


class LLMError(Exception):
    """A failed LLM call. `transient=True` means retrying might work (overload, timeout)."""

    def __init__(self, message: str, *, transient: bool, latency_ms: int = 0):
        super().__init__(message)
        self.transient = transient
        self.latency_ms = latency_ms


class Extractor(Protocol):
    def generate(
        self, *, file_bytes: bytes, mime_type: str, prompt: str, schema: dict[str, Any], model: str
    ) -> LLMResponse: ...


class GeminiExtractor:
    def __init__(self, api_key: str, *, timeout_s: int = 60, thinking_level: str = "low"):
        # A missing key must not turn uploads into HTTP 500s (rule 8): the upload is
        # stored, each call fails as a permanent LLMError, the job ends extraction_failed
        # and POST /invoices/{id}/retry works once the key is configured.
        self._client = (
            genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_s * 1000))
            if api_key
            else None
        )
        self._thinking_level = thinking_level

    def generate(
        self, *, file_bytes: bytes, mime_type: str, prompt: str, schema: dict[str, Any], model: str
    ) -> LLMResponse:
        if self._client is None:
            raise LLMError("GEMINI_API_KEY is not set", transient=False)
        start = time.monotonic()
        uploaded = None
        try:
            if len(file_bytes) > INLINE_LIMIT_BYTES:
                uploaded = self._client.files.upload(
                    file=io.BytesIO(file_bytes), config=types.UploadFileConfig(mime_type=mime_type)
                )
                file_part: Any = uploaded
            else:
                file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)

            resp = self._client.models.generate_content(
                model=model,
                contents=[file_part, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    temperature=0,
                    thinking_config=types.ThinkingConfig(thinking_level=self._thinking_level),
                ),
            )
        except errors.APIError as e:
            raise LLMError(
                f"{e.code}: {str(e)[:500]}",
                transient=is_transient(e.code, str(e)),
                latency_ms=_ms_since(start),
            ) from e
        except TIMEOUT_ERRORS as e:
            raise LLMError("timeout", transient=True, latency_ms=_ms_since(start)) from e
        except TRANSPORT_ERRORS as e:  # network down, DNS failure, server disconnected: retryable
            raise LLMError(
                f"network: {type(e).__name__}: {e}"[:500], transient=True, latency_ms=_ms_since(start)
            ) from e
        except Exception as e:  # anything else the SDK throws must still end extraction_failed (rule 8)
            log_event(log, logging.ERROR, "llm_unexpected_error", model=model, error=type(e).__name__, exc_info=True)
            raise LLMError(
                f"unexpected: {type(e).__name__}: {e}"[:500], transient=False, latency_ms=_ms_since(start)
            ) from e
        finally:
            if uploaded is not None:
                try:
                    self._client.files.delete(name=uploaded.name)
                except Exception:
                    pass  # best effort; Gemini auto-expires uploaded files anyway

        usage = resp.usage_metadata
        return LLMResponse(
            text=resp.text,
            model=model,
            latency_ms=_ms_since(start),
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=usage.candidates_token_count if usage else None,
            thinking_tokens=usage.thoughts_token_count if usage else None,
        )


def is_transient(code: int, message: str) -> bool:
    """Could the SAME model succeed if we retry it soon?"""
    if code == 429 and DAILY_QUOTA.search(message):
        return False
    return code in TRANSIENT_CODES


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
