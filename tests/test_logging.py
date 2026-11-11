"""Structured logging: the formatters, and the per-extraction summary event."""

import json
import logging
import uuid
from decimal import Decimal

from app.extraction.client import LLMError
from app.logging_setup import JsonFormatter, TextFormatter, log_event
from tests.conftest import PDF_BYTES


def _record(event="extraction_finished", **fields) -> logging.LogRecord:
    logger = logging.getLogger("test")
    record = logger.makeRecord("test", logging.INFO, __file__, 1, event, (), None)
    record.fields = fields
    return record


def test_json_formatter_one_object_per_line():
    inv_id = uuid.uuid4()
    line = JsonFormatter().format(_record(invoice_id=inv_id, cost_usd=Decimal("0.0031"), flags=["a"]))
    entry = json.loads(line)
    assert "\n" not in line
    assert entry["event"] == "extraction_finished" and entry["level"] == "info"
    assert entry["invoice_id"] == str(inv_id)
    assert entry["cost_usd"] == "0.0031"  # Decimal stays exact (a string), never a float
    assert entry["flags"] == ["a"]


def test_text_formatter_key_values():
    line = TextFormatter().format(_record(status="approved", llm_calls=1))
    assert line.endswith("extraction_finished status=approved llm_calls=1")


def test_exception_is_included():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger = logging.getLogger("test")
        record = logger.makeRecord("test", logging.ERROR, __file__, 1, "x", (), exc_info=__import__("sys").exc_info())
    assert "RuntimeError: boom" in json.loads(JsonFormatter().format(record))["exception"]


def _finished(caplog) -> list[dict]:
    return [r.fields for r in caplog.records if r.getMessage() == "extraction_finished"]


def _upload(client):
    return client.post("/invoices", files={"file": ("inv.pdf", PDF_BYTES, "application/pdf")})


def test_one_summary_event_per_extraction(client, caplog):
    caplog.set_level(logging.INFO)
    _upload(client)
    [event] = _finished(caplog)
    assert event["status"] == "approved"
    assert event["llm_calls"] == 1 and event["failed_calls"] == 0
    assert event["model"] == "main-model" and event["prompt_version"] == "v3"
    assert event["input_tokens"] == 100 and event["output_tokens"] == 50
    assert event["flags"] == []


def test_summary_counts_retries_and_failures(client, fake_llm, caplog):
    caplog.set_level(logging.INFO)
    fake_llm.replies = [LLMError("503", transient=True)] * 6
    _upload(client)
    [event] = _finished(caplog)
    assert event["status"] == "extraction_failed"
    assert event["llm_calls"] == 6 and event["failed_calls"] == 6 and event["model"] is None
    assert sum(r.getMessage() == "llm_call_failed" for r in caplog.records) == 6


def test_invalid_output_is_logged(client, fake_llm, caplog):
    caplog.set_level(logging.INFO)
    fake_llm.replies = ['{"total": "abc"}']
    _upload(client)
    [warning] = [r for r in caplog.records if r.getMessage() == "llm_output_invalid"]
    assert warning.fields["error_count"] == 1
    [event] = _finished(caplog)
    assert event["llm_calls"] == 2 and event["status"] == "approved"


def test_log_event_helper_attaches_fields(caplog):
    caplog.set_level(logging.INFO)
    log_event(logging.getLogger("x"), logging.INFO, "hello", n=1)
    assert caplog.records[-1].fields == {"n": 1}
