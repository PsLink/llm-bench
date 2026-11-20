#!/usr/bin/env python3
"""Export BFCL request_events.jsonl lengths and successful-request buckets.

Usage:
    python3 scripts/export_bfcl_request_lengths.py request_events.jsonl
    python3 scripts/export_bfcl_request_lengths.py request_events.jsonl \
        --buckets 256,512,1024,2048,4096,8192 --output-dir ./token-stats

Buckets are [lower, upper). Percentages use the valid successful samples of
each metric, not all requests. Missing usage stays blank, rather than zero.
Input tokens already include cached input tokens. Each CSV row represents an
HTTP attempt; request_index includes tool continuations, not just user turns.
"""

import argparse
from bisect import bisect_right
import csv
import json
from pathlib import Path


METRICS = (
    "prompt_tokens", "completion_tokens", "cached_input_tokens",
    "uncached_input_tokens",
)
FIELDS = (
    "attempt_id", "case_id", "request_index", "http_attempt", "status",
    *METRICS,
)


def bucket_edges(value):
    try:
        edges = [int(part) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("桶边界必须是逗号分隔的正整数") from exc
    if not edges or any(edge <= 0 for edge in edges) or edges != sorted(set(edges)):
        raise argparse.ArgumentTypeError("桶边界必须为严格递增的正整数")
    return edges


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="一次运行的 request_events.jsonl")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--buckets", type=bucket_edges,
        default=bucket_edges("128,256,512,1024,2048,4096,8192,16384,32768,65536"),
    )
    args = parser.parse_args()
    destination = args.output_dir or args.source.parent / "token-stats"
    # Validate the entire input before creating output files.
    rows = []
    with args.source.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("phase", "generate") != "generate":
                continue
            for field in METRICS[:-1]:
                value = event.get(field)
                if value is not None and (type(value) is not int or value < 0):
                    parser.error(f"第 {line_number} 行 {field} 不是非负整数或 null")
            row = {field: event.get(field) for field in FIELDS}
            prompt, cached = row["prompt_tokens"], row["cached_input_tokens"]
            if prompt is not None and cached is not None:
                if cached > prompt:
                    parser.error(f"第 {line_number} 行 cached_input_tokens 大于 prompt_tokens")
                row["uncached_input_tokens"] = prompt - cached
            rows.append(row)

    destination.mkdir(parents=True, exist_ok=True)
    lengths_path = destination / "request_lengths.csv"
    with lengths_path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    successful = [row for row in rows if row["status"] == "success"]
    print(f"正式 HTTP 尝试数：{len(rows)}；成功：{len(successful)}；失败/其他：{len(rows) - len(successful)}")
    labels = [f"[{lower}, {upper})" for lower, upper in zip([0] + args.buckets, args.buckets)]
    labels.append(f"[{args.buckets[-1]}, +inf)")
    bucket_rows = []
    for metric in METRICS:
        values = [row[metric] for row in successful if row[metric] is not None]
        counts = [0] * len(labels)
        totals = [0] * len(labels)
        for value in values:
            index = bisect_right(args.buckets, value)
            counts[index] += 1
            totals[index] += value
        missing = len(successful) - len(values)
        print(f"\n{metric}：有效 {len(values)}，缺报 {missing}，总 token {sum(values):,}")
        for label, count, total in zip(labels, counts, totals):
            percent = count / len(values) * 100 if values else None
            bucket_rows.append({
                "metric": metric, "bucket": label, "request_count": count,
                "request_percent": percent, "token_total": total,
                "valid_samples": len(values), "missing_samples": missing,
            })
            percent_text = f"{percent:.2f}%" if percent is not None else "N/A"
            print(f"  {label:<18} {count:>7} 请求  {percent_text:>8}")

    buckets_path = destination / "token_buckets.csv"
    with buckets_path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=list(bucket_rows[0]))
        writer.writeheader()
        writer.writerows(bucket_rows)
    print(f"\n逐请求明细：{lengths_path}\n桶统计：{buckets_path}")


if __name__ == "__main__":
    main()
