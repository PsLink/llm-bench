"""One BFCL invocation owns every concurrency point and every artifact."""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bench.modules.base import BenchmarkCancelled, EndpointConfig
from bench.modules.bfcl_v4 import BFCLV4Module, BFCLV4Params


class BFCLGridTest(unittest.TestCase):
    def execute(self, points, *, bad=None, cancel=None, missing=None, timeout=False):
        seen = []
        roots = []
        def process(command, **kwargs):
            root = Path(command[-1])
            config = json.loads((root / "config.json").read_text())
            concurrency = config["concurrency"]
            self.assertEqual(concurrency, config["manifest"]["concurrency"])
            self.assertLessEqual(config["manifest"]["concurrency"], 128)
            roots.append(root)
            if command[-3] == "evaluate":
                seen.append(concurrency)
                if cancel == concurrency:
                    raise BenchmarkCancelled("canceled")
                if bad == concurrency:
                    raise RuntimeError("failed point")
                metrics = {"accuracy": 0.5 if concurrency == 8 else 0.9,
                           "total_tpm": concurrency * 1000, "ttft_coverage": 1.0}
                if missing == concurrency:
                    del metrics["total_tpm"]
                (root / "summary.json").write_text(json.dumps({"metrics": metrics}))
        params = BFCLV4Params(concurrency_grid=points, max_total_seconds=1 if timeout else None)
        with tempfile.TemporaryDirectory() as output, patch.dict(
            os.environ, {"BFCL_PYTHON": sys.executable}
        ), patch("bench.modules.bfcl_v4.run_process", side_effect=process):
            endpoint = EndpointConfig("http://localhost/v1", "model", "secret", run_context={
                "metric_configs": [{"key": "accuracy", "role": "redline", "min_val": 0.8}]})
            try:
                result = BFCLV4Module().run(endpoint, params, output, cancel_event=threading.Event())
            except BenchmarkCancelled as exc:
                result = exc.partial_result
            index = Path(result.artifacts[0])
            self.assertTrue(index.exists())
            artifact = json.loads((index.parent / "scan.json").read_text())
            self.assertEqual(artifact["concurrency_grid"], points)
            self.assertNotIn("secret", "".join(p.read_text() for p in index.parent.rglob("*.json")))
            self.assertFalse(list(index.parent.rglob("config.json")))
            return result, seen, artifact

    def test_one_module_runs_every_point_and_selects_fastest_accurate_point(self):
        result, seen, artifact = self.execute([1, 3, 5, 6, 8])
        self.assertIsNone(result.error)
        self.assertEqual(seen, [1, 3, 5, 6, 8])
        self.assertEqual(result.metrics["reported_concurrency"], 6)
        self.assertEqual(result.metrics["total_tpm"], 6000)
        self.assertEqual(result.metrics["c8"]["total_tpm"], 8000)
        self.assertEqual(result.metrics["c8"]["eligible"], 0)
        self.assertEqual(result.metrics["sweep_complete"], 1)
        self.assertTrue(artifact["protocol_hash"])

    def test_execution_failure_and_missing_metrics_keep_points_but_do_not_rank(self):
        for kwargs in ({"bad": 3}, {"missing": 3}):
            with self.subTest(kwargs=kwargs):
                result, seen, _ = self.execute([1, 3, 8], **kwargs)
                self.assertEqual(seen, [1, 3, 8])
                self.assertEqual(result.metrics["sweep_complete"], 0)
                self.assertEqual(result.metrics["c3"]["valid"], 0)
                self.assertIsNotNone(result.error)

    def test_cancel_and_total_budget_preserve_partial_artifacts(self):
        result, seen, _ = self.execute([1, 3, 8], cancel=3)
        self.assertEqual(seen, [1, 3])
        self.assertEqual(result.metrics["sweep_complete"], 0)
        self.assertIn("c1", result.metrics)
        result, seen, _ = self.execute([1, 3, 8], timeout=True)
        self.assertEqual(seen, [])
        self.assertIn("budget", result.error)

    def test_grid_validation_and_worker_budget(self):
        for grid in ([1, 1], [3, 1], [0, 4], [1, 129], [1], [1.0, 2], [True, 2]):
            with self.subTest(grid=grid), self.assertRaises(ValueError):
                BFCLV4Params(concurrency_grid=grid)
        params = BFCLV4Params(concurrency_grid=[1, 4, 8], max_seconds=60)
        self.assertEqual(BFCLV4Module.time_budget_seconds(params), 180)
