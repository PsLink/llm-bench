"""One BFCL invocation owns every concurrency point and every artifact."""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bench.modules.base import BenchmarkCancelled, BenchmarkTimeout, EndpointConfig, ModuleResult
from bench.modules.bfcl_v4 import BFCLV4Module, BFCLV4Params


class BFCLGridTest(unittest.TestCase):
    def execute(self, points, *, bad=None, cancel=None, missing=None, timeout=False,
                failure_type=RuntimeError, accuracy=None):
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
                    raise failure_type("failed point")
                metrics = {"accuracy": accuracy if accuracy is not None else (0.5 if concurrency == 8 else 0.9),
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

    def test_partial_failures_and_missing_metrics_keep_the_best_eligible_point(self):
        for kwargs in ({"bad": 3}, {"missing": 3}, {"bad": 3, "failure_type": BenchmarkTimeout}):
            with self.subTest(kwargs=kwargs):
                result, seen, _ = self.execute([1, 3, 8], **kwargs)
                self.assertEqual(seen, [1, 3, 8])
                self.assertEqual(result.metrics["sweep_complete"], 0)
                self.assertEqual(result.metrics["c3"]["valid"], 0)
                self.assertIsNone(result.error)
                self.assertEqual(result.metrics["reported_concurrency"], 1)
                self.assertEqual(result.metrics["sweep_canceled"], 0)
                self.assertEqual(result.metrics["sweep_points_completed"], 2)
                self.assertIn("error", result.metrics["c3"])

    def test_cancel_and_total_budget_preserve_partial_artifacts(self):
        result, seen, _ = self.execute([1, 3, 8], cancel=3)
        self.assertEqual(seen, [1, 3])
        self.assertEqual(result.metrics["sweep_complete"], 0)
        self.assertIn("c1", result.metrics)
        self.assertEqual(result.metrics["sweep_canceled"], 1)
        self.assertEqual(result.metrics["c3"]["valid"], 0)
        result, seen, _ = self.execute([1, 3, 8], timeout=True)
        self.assertEqual(seen, [])
        self.assertIn("budget", result.error)

    def test_grid_validation_and_worker_budget(self):
        for grid in ([1, 1], [3, 1], [0, 4], [1, 129], [1], [1.0, 2], [True, 2]):
            with self.subTest(grid=grid), self.assertRaises(ValueError):
                BFCLV4Params(concurrency_grid=grid)
        params = BFCLV4Params(concurrency_grid=[1, 4, 8], max_seconds=60)
        self.assertEqual(BFCLV4Module.time_budget_seconds(params), 180)


    def test_total_budget_can_keep_an_earlier_eligible_point(self):
        point = ModuleResult(metrics={"accuracy": 0.9, "total_tpm": 1000})
        with tempfile.TemporaryDirectory() as output, patch.object(
            BFCLV4Module, "_run_single", return_value=point
        ), patch("bench.modules.bfcl_v4.time.monotonic", side_effect=[0, 0, 99999]):
            result = BFCLV4Module().run(EndpointConfig("http://localhost/v1", "m"),
                                       BFCLV4Params(concurrency_grid=[1, 3, 8]), output)
        self.assertIsNone(result.error)
        self.assertEqual(result.metrics["reported_concurrency"], 1)
        self.assertEqual(result.metrics["sweep_complete"], 0)
        self.assertEqual(result.metrics["sweep_timed_out"], 1)
        self.assertEqual(result.metrics["sweep_canceled"], 0)

    def test_no_valid_or_no_eligible_point_does_not_pass(self):
        from bench.modules.evaluator import evaluate

        result, _, _ = self.execute([3, 4], bad=3, missing=4)
        self.assertIsNotNone(result.error)
        result, _, _ = self.execute([1, 3, 8], accuracy=0.5)
        _, passed = evaluate(result.metrics, [{"key": "accuracy", "role": "redline", "min_val": 0.8}])
        self.assertFalse(passed)

    def test_worker_deadline_is_partial_timeout_and_manual_cancel_is_not(self):
        for worker_timeout in (False, True):
            with self.subTest(worker_timeout=worker_timeout):
                abort = threading.Event()
                abort.bfcl_timeout_event = threading.Event()
                def point(endpoint, params, *args):
                    if params.concurrency == 3:
                        if worker_timeout:
                            abort.bfcl_timeout_event.set()
                        abort.set()
                        raise BenchmarkCancelled("stopped")
                    return ModuleResult(metrics={"accuracy": 0.9, "total_tpm": 1000})
                with tempfile.TemporaryDirectory() as output, patch.object(
                    BFCLV4Module, "_run_single", side_effect=point
                ):
                    if worker_timeout:
                        result = BFCLV4Module().run(
                            EndpointConfig("http://localhost/v1", "m"),
                            BFCLV4Params(concurrency_grid=[1, 3, 8]), output,
                            cancel_event=abort)
                        self.assertIsNone(result.error)
                    else:
                        with self.assertRaises(BenchmarkCancelled) as caught:
                            BFCLV4Module().run(
                                EndpointConfig("http://localhost/v1", "m"),
                                BFCLV4Params(concurrency_grid=[1, 3, 8]), output,
                                cancel_event=abort)
                        result = caught.exception.partial_result
                self.assertEqual(result.metrics["sweep_canceled"], int(not worker_timeout))
                self.assertEqual(result.metrics["sweep_timed_out"], int(worker_timeout))
                self.assertEqual(result.metrics["sweep_points_completed"], 1)
                self.assertEqual(result.metrics["sweep_points_attempted"], 2)
                self.assertEqual(result.metrics["reported_concurrency"], 1)
