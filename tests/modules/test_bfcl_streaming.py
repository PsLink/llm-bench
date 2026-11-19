import unittest

from bench.bfcl.streaming import assemble_stream


class StreamTests(unittest.TestCase):
    def test_parallel_calls_and_trailing_usage(self):
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {
                "choices": [
                    {"index": 0, "delta": {"reasoning_content": "think", "content": ""}}
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "b",
                                    "type": "function",
                                    "function": {"name": "second", "arguments": "{"},
                                },
                                {
                                    "index": 0,
                                    "id": "a",
                                    "type": "function",
                                    "function": {"name": "first", "arguments": "{"},
                                },
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": "hello",
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "}"}},
                                {"index": 1, "function": {"arguments": '"x":1}'}},
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 7},
                },
            },
        ]
        response, metrics = assemble_stream(
            chunks, started=0, clock=iter([1, 2, 3, 4, 5, 6]).__next__
        )
        message = response["choices"][0]["message"]
        self.assertEqual([c["id"] for c in message["tool_calls"]], ["a", "b"])
        self.assertEqual(
            message["tool_calls"][1]["function"],
            {"name": "second", "arguments": '{"x":1}'},
        )
        self.assertEqual(message["content"], "hello")
        self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(metrics["ttft_seconds"], 3)
        self.assertEqual(metrics["cached_input_tokens"], 7)
        self.assertEqual(metrics["latency_seconds"], 6)

    def test_no_usage_is_null_in_metrics_but_compatible_in_response(self):
        response, metrics = assemble_stream(
            [{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}],
            started=0,
            clock=lambda: 1,
        )
        self.assertIsNone(metrics["prompt_tokens"])
        self.assertIsNone(metrics["ttft_seconds"])
        self.assertEqual(response["usage"]["prompt_tokens"], 0)

    def test_unfinished_stream_is_rejected(self):
        with self.assertRaises(ValueError):
            assemble_stream(
                [{"choices": [{"index": 0, "delta": {"content": "partial"}}]}]
            )
