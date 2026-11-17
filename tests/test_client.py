"""Which Gemini errors are worth retrying on the same model."""

import httpx
import httpx2
import pytest
from google.genai import errors

from app.extraction.client import GeminiExtractor, LLMError, is_transient

# Shapes of real 429 messages from the first eval run (trimmed).
PER_MINUTE = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generate_content_free_tier_requests, "
    "limit: 5, model: gemini-3.6-flash, description: Generate content requests per minute "
    "limit exceeded on FreeTier"
)
PER_DAY = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generate_content_free_tier_requests, "
    "limit: 20, model: gemini-3.6-flash, description: Generate content requests per day "
    "limit exceeded on FreeTier"
)


def test_per_minute_rate_limit_is_retried():
    assert is_transient(429, PER_MINUTE)


def test_daily_quota_is_not_retried():
    assert not is_transient(429, PER_DAY)
    assert not is_transient(429, "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")


def test_overload_and_server_errors_are_retried():
    assert is_transient(503, "UNAVAILABLE")
    assert is_transient(500, "INTERNAL")


def test_bad_request_is_not_retried():
    assert not is_transient(400, "INVALID_ARGUMENT")


def test_real_api_error_string_is_classified():
    """str(APIError) is what GeminiExtractor passes in: make sure the text survives."""
    e = errors.APIError(429, {"error": {"code": 429, "message": PER_DAY, "status": "RESOURCE_EXHAUSTED"}})
    assert not is_transient(e.code, str(e))


def test_missing_api_key_is_a_permanent_llm_error_not_a_crash():
    """Rule 8: a missing key must end as extraction_failed, never an HTTP 500."""
    extractor = GeminiExtractor("")  # constructing it must not raise
    with pytest.raises(LLMError) as err:
        extractor.generate(file_bytes=b"x", mime_type="application/pdf", prompt="p", schema={}, model="m")
    assert not err.value.transient and "GEMINI_API_KEY" in str(err.value)

_REQ = httpx.Request("POST", "https://example.invalid")
_REQ2 = httpx2.Request("POST", "https://example.invalid")


class _Raising:
    """Stands in for genai.Client: generate_content raises the given exception."""

    def __init__(self, exc: Exception):
        self.models = self
        self._exc = exc

    def generate_content(self, **kw):
        raise self._exc


@pytest.mark.parametrize(
    ("exc", "transient", "prefix"),
    [
        # google-genai 3.x uses httpx2, whose errors are not httpx errors: these used to escape as a 500.
        (httpx2.RemoteProtocolError("Server disconnected", request=_REQ2), True, "network:"),
        (httpx2.ConnectError("refused", request=_REQ2), True, "network:"),
        (httpx2.ReadTimeout("slow", request=_REQ2), True, "timeout"),
        (httpx.ConnectError("refused", request=_REQ), True, "network:"),
        (httpx.ReadTimeout("slow", request=_REQ), True, "timeout"),
        (ValueError("weird SDK bug"), False, "unexpected:"),
    ],
    ids=lambda v: type(v).__module__ + "." + type(v).__name__ if isinstance(v, Exception) else None,
)
def test_sdk_exceptions_become_llm_errors_not_crashes(exc, transient, prefix):
    """Rule 8: whatever the SDK raises ends as an LLMError (→ extraction_failed), never a 500."""
    extractor = GeminiExtractor("fake-key")
    extractor._client = _Raising(exc)
    with pytest.raises(LLMError) as err:
        extractor.generate(file_bytes=b"x", mime_type="application/pdf", prompt="p", schema={}, model="m")
    assert err.value.transient is transient
    assert str(err.value).startswith(prefix)
    assert err.value.__cause__ is exc
