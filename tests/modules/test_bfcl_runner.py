import json
from importlib.util import find_spec
import tempfile
import unittest
from pathlib import Path
from bench.bfcl.runner import score_results
from bench.bfcl.telemetry import aggregate_metrics, EventWriter
from bench.bfcl.trace import TraceWriter


class RunnerTests(unittest.TestCase):
    def test_score_failure_rows_define_case_results_with_exact_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            score = root / "BFCL_v4_simple_python_score.json"
            score.write_text(
                json.dumps({"total_count": 2, "correct_count": 1, "accuracy": 0.5})
                + "\n"
                + json.dumps({"id": "simple_python_1", "valid": False})
                + "\n"
            )
            rows = score_results(
                root, {"simple_python": ["simple_python_0", "simple_python_1"]}
            )
            self.assertEqual([r["success"] for r in rows], [True, False])
            with self.assertRaises(ValueError):
                score_results(root, {"simple_python": ["simple_python_0"]})
            score.write_text(
                json.dumps({"total_count": 2, "correct_count": 1, "accuracy": 0.5})
                + "\n"
                + json.dumps({"id": "alien", "valid": False})
                + "\n"
            )
            with self.assertRaises(ValueError):
                score_results(
                    root, {"simple_python": ["simple_python_0", "simple_python_1"]}
                )

    def test_aggregation_missing_usage_is_not_zero(self):
        metrics = aggregate_metrics(
            [
                {
                    "status": "success",
                    "latency_seconds": 2,
                    "ttft_seconds": 1,
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "cached_input_tokens": None,
                },
                {
                    "status": "error",
                    "latency_seconds": 4,
                    "ttft_seconds": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "cached_input_tokens": None,
                },
            ]
        )
        self.assertEqual(metrics["request_count"], 2)
        self.assertEqual(metrics["request_error_count"], 1)
        self.assertIsNone(metrics["prompt_tokens_total"])
        self.assertEqual(metrics["usage_coverage"], 0.5)

    def test_trace_strips_credentials_and_has_hash_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = TraceWriter(root, "protocol")
            writer.write(
                {
                    "model": "m",
                    "messages": [{"role": "user", "content": "hello"}],
                    "api_key": "secret",
                    "extra_headers": {"Authorization": "secret"},
                },
                {"case_id": "a", "request_index": 0},
            )
            from bench.replay_test.log_replay_tool import load_extract_jsonl

            record = next(load_extract_jsonl(str(root / "trace.jsonl")))
            body = json.loads(record.request_body)
            self.assertNotIn("api_key", body)
            self.assertNotIn("extra_headers", body)
            meta = json.loads((root / "trace-index.jsonl").read_text())
            self.assertEqual(meta["protocol_hash"], "protocol")
            self.assertEqual(len(meta["body_sha256"]), 64)

    def test_event_writer_keeps_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request_events.jsonl"
            writer = EventWriter(path)
            writer.write({"case_id": "a", "request_index": 0})
            writer.write({"case_id": "a", "request_index": 1})
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_trace_preserves_tool_arguments_named_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            TraceWriter(root, "p").write(
                {
                    "tools": [
                        {
                            "function": {
                                "parameters": {
                                    "properties": {"token": {"type": "string"}}
                                }
                            }
                        }
                    ]
                },
                {},
            )
            self.assertIn("token", (root / "trace.jsonl").read_text())

    def test_metric_coverage_aliases(self):
        metrics = aggregate_metrics(
            [
                {
                    "status": "success",
                    "ttft_seconds": 1,
                    "latency_seconds": 2,
                    "prompt_tokens": 4,
                    "completion_tokens": None,
                    "cached_input_tokens": 0,
                }
            ]
        )
        self.assertEqual(metrics["ttft_coverage"], 1)
        self.assertEqual(metrics["input_usage_coverage"], 1)
        self.assertEqual(metrics["output_usage_coverage"], 0)
        self.assertEqual(metrics["cache_usage_coverage"], 1)

    @unittest.skipUnless(
        find_spec("openai") and find_spec("httpx"), "requires the isolated BFCL SDK"
    )
    def test_official_inference_boundary_links_multiple_requests(self):
        import httpx
        from openai import OpenAI
        from bench.bfcl.runner import handler_class, write_probe_report

        class Base:
            def inference(self, entry, include_input_log, exclude_state_log):
                return [
                    self.generate_with_backoff(model="served", messages=[])[0]
                    for _ in range(2)
                ]

        def serve(request):
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            self.assertEqual(body["stream_options"], {"include_usage": True})
            chunks = [
                {
                    "id": "x",
                    "created": 0,
                    "model": "served",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                + "data: [DONE]\n\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Handler = handler_class(
                Base,
                {"manifest": {}, "endpoint": "http://localhost:99/v1", "timeout": 3},
                root,
                phase="probe",
            )
            handler = Handler()
            options = handler._build_client_kwargs()
            self.assertEqual(options["max_retries"], 0)
            self.assertEqual(options["timeout"], 3)
            options["http_client"].close()
            handler.client = OpenAI(
                api_key="unused",
                max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(serve)),
            )
            responses = handler.inference({"id": "a"}, False, False)
            self.assertEqual(responses[0].choices[0].message.content, "ok")
            events = [
                json.loads(line)
                for line in (root / "request_events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [(e["case_id"], e["request_index"]) for e in events],
                [("a", 0), ("a", 1)],
            )
            self.assertTrue(all(e["prompt_tokens"] is None for e in events))
            write_probe_report(root, "a")
            report = json.loads((root / "probe.json").read_text())
            self.assertEqual(report["successful_requests"], 2)
            self.assertFalse(report["tool_call_observed"])
            self.assertFalse(report["tool_result_roundtrip_observed"])
            self.assertFalse(report["stream_usage_observed"])
            handler.client.close()

    @unittest.skipUnless(
        find_spec("openai") and find_spec("httpx"), "requires the isolated BFCL SDK"
    )
    def test_rate_limit_has_one_attempt_and_sanitized_error(self):
        import httpx
        from openai import OpenAI
        from bench.bfcl.runner import handler_class

        count = []

        class Base:
            def inference(self, entry, *args):
                return self.generate_with_backoff(model="served", messages=[])

        def serve(request):
            count.append(1)
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "secret http://sensitive.host",
                        "type": "rate_limit",
                    }
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = handler_class(
                Base,
                {"manifest": {}, "endpoint": "http://localhost/v1", "timeout": 3},
                root,
            )()
            handler.client = OpenAI(
                api_key="secret",
                max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(serve)),
            )
            with self.assertRaisesRegex(
                RuntimeError, "^BFCL request failed: RateLimitError$"
            ):
                handler.inference({"id": "a"}, False, False)
            self.assertEqual(len(count), 1)
            event = json.loads((root / "request_events.jsonl").read_text())
            self.assertEqual(event["http_status"], 429)
            self.assertNotIn("secret", json.dumps(event))
            self.assertNotIn("http://", json.dumps(event))
            handler.client.close()

    @unittest.skipUnless(
        find_spec("openai") and find_spec("httpx"), "requires the isolated BFCL SDK"
    )
    def test_retry_reuses_case_and_request_index(self):
        import httpx
        from openai import OpenAI
        from unittest.mock import patch
        from bench.bfcl.runner import handler_class

        attempts = []

        class Base:
            def inference(self, entry, *args):
                return self.generate_with_backoff(model="served", messages=[])

        chunk = {
            "id": "x",
            "created": 0,
            "model": "served",
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}
            ],
        }

        def serve(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(503, json={"error": {"message": "busy"}})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "manifest": {},
                "endpoint": "http://localhost/v1",
                "timeout": 3,
                "request_retries": 1,
            }
            handler = handler_class(Base, config, root)()
            with OpenAI(
                api_key="unused",
                max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(serve)),
            ) as client:
                handler.client = client
                with patch("bench.bfcl.runner.time.sleep"):
                    handler.inference({"id": "retry"}, False, False)
            events = [
                json.loads(line)
                for line in (root / "request_events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [
                    (e["request_index"], e["http_attempt"], e["http_status"])
                    for e in events
                ],
                [(0, 1, 503), (0, 2, 200)],
            )
            self.assertEqual(aggregate_metrics(events)["infra_error_count"], 0)
