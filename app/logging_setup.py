"""Structured logging with the standard library (no extra dependency).

Each important moment is one EVENT with named fields, not a sentence:

    log_event(log, logging.INFO, "extraction_finished", invoice_id=..., status=..., cost_usd=...)

LOG_FORMAT=json (production image): one JSON object per line, so a log system can
filter and aggregate (e.g. "p95 latency of extractions that ended needs_review").
LOG_FORMAT=text (dev default): the same fields as key=value, readable in a terminal.
"""

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

_FIELDS = "fields"  # LogRecord attribute that carries the event's fields


def log_event(
    logger: logging.Logger, level: int, event: str, *, exc_info: bool = False, **fields: Any
) -> None:
    logger.log(level, event, exc_info=exc_info, extra={_FIELDS: fields})


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)  # exact, never a float
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # UUID, date, enum...


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
            **{k: _jsonable(v) for k, v in getattr(record, _FIELDS, {}).items()},
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(f"{k}={_jsonable(v)}" for k, v in getattr(record, _FIELDS, {}).items())
        line = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        line = f"{line} {fields}" if fields else line
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(fmt: str = "text", level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    adopt_uvicorn_loggers()


def adopt_uvicorn_loggers() -> None:
    """uvicorn installs its own text handlers; send its logs through ours instead so the
    whole stream has one format (JSON in production).

    Called again at app startup: `fastapi run` imports the app (running
    configure_logging) BEFORE uvicorn applies its log config, which would undo this.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True
