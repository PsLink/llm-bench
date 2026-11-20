import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/export_bfcl_request_lengths.py"


class ExportBFCLRequestLengthsTests(unittest.TestCase):
    def export(self, events):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "request_events.jsonl"
            source.write_text("".join(json.dumps(event) + "\n" for event in events))
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(source), "--buckets", "128,256"],
                capture_output=True, text=True, check=True,
            )
            with (Path(directory) / "token-stats/token_buckets.csv").open(
                encoding="utf-8-sig", newline="",
            ) as stream:
                buckets = list(csv.DictReader(stream))
            return result.stdout, buckets

    def test_all_cache_usage_missing_has_unknown_totals(self):
        stdout, buckets = self.export([
            {"status": "success", "prompt_tokens": 200, "completion_tokens": 10}
            for _ in range(8)
        ])
        for metric in ("cached_input_tokens", "uncached_input_tokens"):
            self.assertIn(f"{metric}：有效 0，缺报 8，总 token N/A", stdout)
            for row in buckets:
                if row["metric"] == metric:
                    self.assertEqual(row["token_total"], "")
                    self.assertEqual(row["valid_samples"], "0")
                    self.assertEqual(row["missing_samples"], "8")

    def test_measured_zero_keeps_numeric_totals_in_all_buckets(self):
        stdout, buckets = self.export([
            {"status": "success", "prompt_tokens": 200, "completion_tokens": 10,
             "cached_input_tokens": 0}
            for _ in range(8)
        ])
        self.assertIn("cached_input_tokens：有效 8，缺报 0，总 token 0", stdout)
        cache = [row for row in buckets if row["metric"] == "cached_input_tokens"]
        self.assertEqual([row["token_total"] for row in cache], ["0", "0", "0"])
        self.assertEqual([row["request_count"] for row in cache], ["8", "0", "0"])

    def test_partial_usage_sums_only_successful_measured_samples(self):
        stdout, buckets = self.export([
            {"status": "success", "prompt_tokens": 200, "cached_input_tokens": 50},
            {"status": "success", "prompt_tokens": 200},
            {"status": "failed", "cached_input_tokens": 100},
        ])
        self.assertIn("cached_input_tokens：有效 1，缺报 1，总 token 50", stdout)
        cache = [row for row in buckets if row["metric"] == "cached_input_tokens"]
        self.assertEqual([row["token_total"] for row in cache], ["50", "0", "0"])
        self.assertTrue(all(row["valid_samples"] == "1" for row in cache))
        self.assertIn("completion_tokens：有效 0，缺报 2，总 token N/A", stdout)


if __name__ == "__main__":
    unittest.main()
