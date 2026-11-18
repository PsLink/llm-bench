"""Extract and aggregate paired cache usage without imputing absent values."""


def cache_usage(usage):
    prompt = usage.get("prompt_tokens")
    if type(prompt) is not int or prompt < 0:
        prompt = None
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    for field in ("cached_tokens", "prompt_cache_hit_tokens"):
        if cached is None:
            cached = usage.get(field)
    if (
        type(cached) is not int
        or cached < 0
        or (prompt is not None and cached > prompt)
    ):
        cached = None
    return prompt, cached


def cache_metrics(rows):
    pairs = [
        (row["prompt_tokens"], row["cached_tokens"])
        for row in rows
        if type(row.get("prompt_tokens")) is int
        and type(row.get("cached_tokens")) is int
        and 0 <= row["cached_tokens"] <= row["prompt_tokens"]
    ]
    input_total = sum(prompt for prompt, _ in pairs)
    cached_total = sum(cached for _, cached in pairs)
    return {
        "cache_hit_rate": cached_total / input_total if input_total else None,
        "cache_usage_coverage": len(pairs) / len(rows) if rows else None,
        "cache_usage_sample_count": len(pairs),
        "cached_input_tokens": cached_total if pairs else None,
    }
