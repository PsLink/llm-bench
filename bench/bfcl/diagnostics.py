"""Bounded, content-free diagnostics shared by the runner and its parent."""

import json
import logging
import time
import traceback
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)


class DiagnosticError(ValueError):
    """Only caller-owned codes and metadata, never upstream exception messages."""

    def __init__(self, message, code, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def exception_details(exc):
    result = {
        "type": type(exc).__name__,
        "frames": [
            {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(exc.__traceback__)[-12:]
        ],
    }
    if isinstance(exc, DiagnosticError):
        result.update(code=exc.code, details=exc.details)
    return result


def save_state(path, state):
    """Diagnostics are best effort and must not replace a benchmark failure."""
    try:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({**state, "updated_at_unix": time.time()}, allow_nan=False))
        temp.replace(path)
    except (OSError, ValueError, TypeError):
        log.warning("BFCL diagnostic_state_write_failed file=%s", path.name)


def read_error(root):
    path = root / "runner_error.json"
    if not path.exists():
        return {"code": "diagnostic_missing"}
    try:
        if path.stat().st_size > 65536:
            raise ValueError("oversized")
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise TypeError("invalid")
        # This file is written by our runner; never consume upstream error bodies.
        return {k: value[k] for k in ("phase", "stage", "type", "code", "details", "frames") if k in value}
    except (OSError, ValueError, TypeError):
        return {"code": "diagnostic_unreadable"}


class EventSummary:
    """Incremental JSONL reader; bounded samples and partial-line handling."""

    def __init__(self, root):
        self.root = root
        self.offsets = {}
        self.counts = Counter()
        self.http = Counter()
        self.errors = Counter()
        self.samples = []
        self.last_event = None
        self.case_samples = []

    def poll(self):
        for name in ("request_events.jsonl", "case_events.jsonl"):
            try:
                with (self.root / name).open("rb") as source:
                    source.seek(self.offsets.get(name, 0))
                    while True:
                        line = source.readline(65537)
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            # Retry partial writes, but skip oversized records in bounded chunks.
                            if len(line) <= 65536:
                                break
                            while line and not line.endswith(b"\n"):
                                line = source.readline(65537)
                            self.offsets[name] = source.tell()
                            self.counts["malformed"] += 1
                            continue
                        self.offsets[name] = source.tell()
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict):
                                raise TypeError("invalid event")
                        except (ValueError, TypeError, UnicodeError):
                            self.counts["malformed"] += 1
                            continue
                        now = time.monotonic()
                        finished = row.get("finished_monotonic")
                        if type(finished) not in (int, float) or not 0 <= finished <= now:
                            finished = now
                        self.last_event = max(self.last_event or 0, finished)
                        if name == "case_events.jsonl":
                            self.counts["cases_finished"] += 1
                            self.counts["cases_failed"] += row.get("status") != "generated"
                            if row.get("status") != "generated" and len(self.case_samples) < 5:
                                self.case_samples.append({k: row[k] for k in
                                    ("case_id", "type", "code", "frames") if k in row})
                            continue
                        self.counts["total"] += 1
                        failed = row.get("status") != "success"
                        self.counts["failed" if failed else "succeeded"] += 1
                        status = row.get("http_status")
                        if type(status) is int and 100 <= status <= 599:
                            self.http[str(status)] += 1
                        error = row.get("error_type")
                        if isinstance(error, str) and error.isidentifier() and len(error) < 80:
                            key = error if error in self.errors or len(self.errors) < 20 else "other"
                            self.errors[key] += 1
                        if failed and len(self.samples) < 5:
                            self.samples.append({k: row[k] for k in (
                                "case_id", "request_index", "http_attempt", "error_type",
                                "http_status", "code", "stage",
                            ) if k in row and isinstance(row[k], (str, int, float))
                                and len(str(row[k])) <= 160})
                            if isinstance(row.get("frames"), list):
                                self.samples[-1]["frames"] = row["frames"][-12:]
            except OSError:
                continue
        return {
            **{k: self.counts[k] for k in ("total", "succeeded", "failed", "cases_finished", "cases_failed", "malformed")},
            "http_statuses": dict(self.http), "error_types": dict(self.errors),
            "failure_samples": list(self.samples), "case_failure_samples": list(self.case_samples),
            "seconds_since_last_event": round(time.monotonic() - self.last_event, 1) if self.last_event is not None else None,
        }
