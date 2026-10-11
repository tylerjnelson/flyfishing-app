"""
JSON log formatter.

Every field passed via `extra={...}` is emitted, so structured context (job,
source, error, site_id, ...) actually reaches the journal and app.log. The
previous printf-template formatter silently dropped all of it, and broke JSON
whenever a message contained a quote.

Secrets are scrubbed from the message and every string field: httpx error
messages embed the full request URL, and several upstream APIs (AirNow, NPS,
HERE) take their key as a query parameter.
"""

import json
import logging
import re
from datetime import datetime, timezone

# Attributes every LogRecord carries; anything else on a record came from `extra`.
_STD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "request_id", "taskName",
}

# key=value pairs in URLs / query strings whose value must never be logged.
_SECRET_RE = re.compile(
    r"((?:api[_-]?key|apikey|access[_-]?token|token|secret|password|signature)=)[^&\s'\"]+",
    re.IGNORECASE,
)


def redact(s: str) -> str:
    return _SECRET_RE.sub(r"\1[REDACTED]", s)


def _clean(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and key not in out:
                out[key] = _clean(value)
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            out["stack"] = redact(self.formatStack(record.stack_info))
        return json.dumps(out, default=str)
