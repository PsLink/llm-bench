"""Assemble native OpenAI tool streams without interpreting tool arguments."""

import time

from bench.bfcl.diagnostics import DiagnosticError
from bench.cache_usage import cache_usage


def _dict(value):
    return value if isinstance(value, dict) else value.model_dump(exclude_none=True)


def assemble_stream(chunks, *, started=None, deadline=None, clock=time.monotonic):
    started = clock() if started is None else started
    choices = {}
    usage = None
    first = None
    metadata = {}
    for raw in chunks:
        now = clock()
        if deadline is not None and now >= deadline:
            raise TimeoutError("BFCL stream exceeded request deadline")
        chunk = _dict(raw)
        for key in ("id", "created", "model", "system_fingerprint"):
            if chunk.get(key) is not None:
                metadata[key] = chunk[key]
        if chunk.get("usage") is not None:
            usage = _dict(chunk["usage"])
        for item in chunk.get("choices", []):
            index = item.get("index", 0)
            choice = choices.setdefault(
                index, {"content": [], "tools": {}, "finish_reason": None}
            )
            delta = item.get("delta") or {}
            content = delta.get("content")
            if content:
                choice["content"].append(content)
                if first is None:
                    first = now
            for tool in delta.get("tool_calls") or []:
                target = choice["tools"].setdefault(
                    tool["index"],
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tool.get("id"):
                    target["id"] += tool["id"]
                if tool.get("type"):
                    target["type"] = tool["type"]
                for field in ("name", "arguments"):
                    value = (tool.get("function") or {}).get(field)
                    if value:
                        target["function"][field] += value
                # Tool IDs alone are structural, not generated output.
                if first is None and any(
                    (tool.get("function") or {}).get(k) for k in ("name", "arguments")
                ):
                    first = now
            if item.get("finish_reason") is not None:
                choice["finish_reason"] = item["finish_reason"]
    if deadline is not None and clock() >= deadline:
        raise TimeoutError("BFCL stream exceeded request deadline")
    if not choices or any(c["finish_reason"] is None for c in choices.values()):
        raise DiagnosticError("Incomplete completion stream", "completion_stream_incomplete")
    result_choices = []
    for index, choice in sorted(choices.items()):
        message = {"role": "assistant", "content": "".join(choice["content"]) or None}
        if choice["tools"]:
            message["tool_calls"] = [
                tool for _, tool in sorted(choice["tools"].items())
            ]
        result_choices.append(
            {
                "index": index,
                "message": message,
                "finish_reason": choice["finish_reason"],
                "logprobs": None,
            }
        )
    prompt_tokens, cached_tokens = cache_usage(usage or {})
    completion_tokens = (usage or {}).get("completion_tokens")
    if type(completion_tokens) is not int or completion_tokens < 0:
        completion_tokens = None
    metrics = {
        "finish_reason": result_choices[0]["finish_reason"],
        "ttft_seconds": None if first is None else first - started,
        "latency_seconds": clock() - started,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_input_tokens": cached_tokens,
    }
    response = {
        "id": "",
        "created": 0,
        "model": "",
        **metadata,
        "object": "chat.completion",
        "choices": result_choices,
        "usage": {
            "prompt_tokens": prompt_tokens or 0,
            "completion_tokens": completion_tokens or 0,
            "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
        },
    }
    return response, metrics
