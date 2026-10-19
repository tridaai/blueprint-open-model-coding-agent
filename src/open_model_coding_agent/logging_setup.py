"""Structured JSON logs with request IDs (house standard).

One line per event on stdout: timestamp, level, event, request id when
the API supplied one, and the event's fields. Deliberately tiny — the
audit table is the durable record; the log is for operators watching
the process.
"""

from __future__ import annotations

import contextvars
import json
import sys
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def log_event(event: str, **fields) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "level": "INFO",
        "event": event,
        "request_id": request_id_var.get(),
    }
    record.update(fields)
    print(json.dumps(record), file=sys.stdout, flush=True)
