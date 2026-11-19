"""Replay JSONL bodies and a separate provenance index; opt-in only."""

import hashlib
import json
import threading

# Transport options never belong in Replay request JSON.
_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "x-api-key",
    "access_token",
    "token",
    "password",
    "cookie",
    "headers",
    "extra_headers",
    "base_url",
    "endpoint",
}


def _safe(value, api_key):
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {key: _safe(item, api_key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item, api_key) for item in value]
    if api_key and isinstance(value, str):
        return value.replace(api_key, "[REDACTED]")
    return value


class TraceWriter:
    def __init__(self, root, protocol_hash, api_key=""):
        self.root = root
        self.protocol_hash = protocol_hash
        self.api_key = api_key
        self.lock = threading.Lock()
        self.line = 0

    def write(self, body, identity):
        payload = _safe(
            {k: v for k, v in body.items() if k.lower() not in _SECRET_KEYS},
            self.api_key,
        )
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        with self.lock:
            self.line += 1
            index = {
                **identity,
                "line": self.line,
                "protocol_hash": self.protocol_hash,
                "body_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            }
            record = {
                "source_file": "bfcl",
                "line_no": self.line,
                "level": "info",
                "timestamp": "",
                "request_id": f"{identity.get('case_id', 'unknown')}:{identity.get('request_index', self.line)}",
                "request_body": encoded,
            }
            with (self.root / "trace.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
            with (self.root / "trace-index.jsonl").open(
                "a", encoding="utf-8"
            ) as output:
                output.write(json.dumps(index) + "\n")
