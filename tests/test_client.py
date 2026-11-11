"""Which Gemini errors are worth retrying on the same model."""

from google.genai import errors

from app.extraction.client import is_transient

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
    import pytest

    from app.extraction.client import GeminiExtractor, LLMError

    extractor = GeminiExtractor("")  # constructing it must not raise
    with pytest.raises(LLMError) as err:
        extractor.generate(file_bytes=b"x", mime_type="application/pdf", prompt="p", schema={}, model="m")
    assert not err.value.transient and "GEMINI_API_KEY" in str(err.value)
