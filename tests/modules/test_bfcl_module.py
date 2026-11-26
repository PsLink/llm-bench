import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bench.modules.base import BenchmarkCancelled, EndpointConfig
from bench.modules.bfcl_v4 import BFCLV4Module, BFCLV4Params, run_process


class BFCLModuleTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"BFCL_PYTHON": sys.executable})
        env.start()
        self.addCleanup(env.stop)

    def test_attempt_isolation_and_no_credentials_in_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = []

            def fake(command, env, deadline, cancel_event):
                root = Path(command[-1])
                roots.append(root)
                self.assertEqual(env["BFCL_ENDPOINT_API_KEY"], "secret-key")
                if command[-3] == "evaluate":
                    (root / "summary.json").write_text(
                        json.dumps({"metrics": {"accuracy": 0.5, "ttft_coverage": 1.0}})
                    )

            module = BFCLV4Module()
            with patch("bench.modules.bfcl_v4.run_process", side_effect=fake):
                results = [
                    module.run(
                        EndpointConfig(
                            "http://localhost:80/v1", "served", "secret-key"
                        ),
                        BFCLV4Params(),
                        directory,
                    )
                    for _ in range(2)
                ]
            self.assertEqual(len(set(roots)), 2)
            for result in results:
                self.assertIsNone(result.error)
                index = Path(result.artifacts[0])
                self.assertTrue(index.is_file())
                self.assertNotIn(
                    "secret-key",
                    "".join(p.read_text() for p in index.parent.rglob("*.json")),
                )
                self.assertFalse((index.parent / "config.json").exists())

    def test_missing_required_metric_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:

            def fake(command, **kwargs):
                (Path(command[-1]) / "summary.json").write_text(
                    '{"metrics":{"accuracy":0.5}}'
                )

            with patch("bench.modules.bfcl_v4.run_process", side_effect=fake):
                result = BFCLV4Module().run(
                    EndpointConfig("http://localhost/v1", "m"),
                    BFCLV4Params(required_metrics=["ttft_p90_ms"]),
                    directory,
                )
            self.assertIn("ttft_p90_ms", result.error)
            self.assertTrue(Path(result.artifacts[0]).exists())

    def test_credential_url_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            result = BFCLV4Module().run(
                EndpointConfig("https://user:pass@host/v1?key=x", "m"),
                BFCLV4Params(),
                directory,
            )
            self.assertIn("credential", result.error)
            self.assertNotIn("pass@", result.error)

    def test_process_cancel_and_deadline(self):
        from bench.modules.base import BenchmarkTimeout

        with self.assertRaises(BenchmarkTimeout):
            run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                env={},
                deadline=time.monotonic() + 0.1,
                cancel_event=None,
            )
        canceled = threading.Event()
        canceled.set()
        with self.assertRaises(BenchmarkCancelled):
            run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                env={},
                deadline=time.monotonic() + 3,
                cancel_event=canceled,
            )

    def test_probe_process_has_separate_configuration_and_runs_first(self):
        phases = []

        def fake(command, **kwargs):
            phase = command[-3]
            root = Path(command[-1])
            phases.append(phase)
            config = json.loads((root / "config.json").read_text())
            if phase == "probe":
                self.assertEqual(root.name, "probe")
                self.assertEqual(config["concurrency"], 1)
                self.assertFalse(config["export_trace"])
                self.assertEqual(
                    config["manifest"]["cases"],
                    {"multi_turn_base": ["multi_turn_base_0"]},
                )
            elif phase == "evaluate":
                (root / "summary.json").write_text('{"metrics":{"accuracy":0.5}}')

        with tempfile.TemporaryDirectory() as directory:
            with patch("bench.modules.bfcl_v4.run_process", side_effect=fake):
                result = BFCLV4Module().run(
                    EndpointConfig("http://localhost/v1", "m"),
                    BFCLV4Params(probe_endpoint=True, export_replay_trace=True),
                    directory,
                )
            self.assertIsNone(result.error)
            self.assertEqual(phases, ["probe", "generate", "evaluate"])
            index = Path(result.artifacts[0])
            self.assertFalse((index.parent / "probe" / "config.json").exists())

    def test_smoke_manifest_cannot_be_expanded(self):
        from pydantic import ValidationError

        with self.assertRaisesRegex(ValidationError, "smoke"):
            BFCLV4Params(task_set="bfcl-v4-smoke-v1", cases_per_category=2)

    def test_smoke_rejects_tail_selection(self):
        from pydantic import ValidationError

        with self.assertRaisesRegex(ValidationError, "smoke.*head"):
            BFCLV4Params(case_selection="tail")
        self.assertEqual(BFCLV4Params().case_selection, "head")
        for task_set in ("bfcl-v4-tool-use-v1", "bfcl-v4-multi-turn-long-context-v1"):
            with self.subTest(task_set=task_set):
                self.assertEqual(BFCLV4Params(task_set=task_set, case_selection="tail").case_selection, "tail")

    def test_long_context_task_set_runs_only_selected_cases(self):
        hashes = set()
        for count in (1, 2, 200):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                manifests = []

                def fake(command, **kwargs):
                    root = Path(command[-1])
                    config = json.loads((root / "config.json").read_text())
                    manifests.append(config["manifest"])
                    if command[-3] == "evaluate":
                        (root / "summary.json").write_text(json.dumps({"metrics": {
                            "accuracy": 1.0,
                            "accuracy_multi_turn_long_context": 1.0,
                            "case_count_multi_turn_long_context": count,
                        }}))

                with patch("bench.modules.bfcl_v4.run_process", side_effect=fake):
                    result = BFCLV4Module().run(
                        EndpointConfig("http://localhost/v1", "m"),
                        BFCLV4Params(
                            task_set="bfcl-v4-multi-turn-long-context-v1",
                            cases_per_category=count,
                            required_metrics=["accuracy_multi_turn_long_context"],
                        ),
                        directory,
                    )
                self.assertIsNone(result.error)
                self.assertEqual(len(manifests), 2)
                for manifest in manifests:
                    self.assertEqual(manifest["cases"], {
                        "multi_turn_long_context": [
                            f"multi_turn_long_context_{index}" for index in range(count)
                        ],
                    })
                self.assertEqual(result.metrics["case_count_multi_turn_long_context"], count)
                hashes.add(manifests[0]["protocol_hash"])
        self.assertEqual(len(hashes), 3)

    def test_long_context_case_count_bounds(self):
        from pydantic import ValidationError

        for count in (0, 201):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                BFCLV4Params(
                    task_set="bfcl-v4-multi-turn-long-context-v1",
                    cases_per_category=count,
                )
