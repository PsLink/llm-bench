"""Run the frozen BFCL V4 subset in its isolated, pinned Python environment."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from bench.modules.base import (
    BenchmarkCancelled,
    BenchmarkTimeout,
    EndpointConfig,
    MetricConfig,
    MetricDescriptor,
    ModuleResult,
    TestModule,
    _noop_progress,
)

MANIFEST_DIR = Path(__file__).parents[1] / "bfcl"
MANIFESTS = {
    "bfcl-v4-smoke-v1": MANIFEST_DIR / "smoke-manifest.json",
    "bfcl-v4-tool-use-v1": MANIFEST_DIR / "tool-use-manifest.json",
}


# In the pinned BFCL revision, each of the five tool-use categories has
# contiguous zero-based IDs and at least 200 official cases.
MAX_TOOL_USE_CASES_PER_CATEGORY = 200


class BFCLV4Params(BaseModel):
    task_set: Literal["bfcl-v4-smoke-v1", "bfcl-v4-tool-use-v1"] = "bfcl-v4-smoke-v1"
    cases_per_category: int = Field(
        1,
        ge=1,
        le=MAX_TOOL_USE_CASES_PER_CATEGORY,
        description="Number of cases per category for bfcl-v4-tool-use-v1, selected by ascending case ID.",
    )
    concurrency: int = Field(2, ge=1, le=128)
    request_timeout_seconds: float = Field(120, ge=1, le=3600)
    max_seconds: float = Field(1800, ge=1, le=86400)
    request_retries: int = Field(0, ge=0, le=3)
    export_replay_trace: bool = False
    probe_endpoint: bool = Field(
        False,
        description="Run one official multi-turn generation before the measured run.",
    )
    required_metrics: list[str] = Field(default_factory=lambda: ["accuracy"])
    min_ttft_coverage: float = Field(0, ge=0, le=1)
    min_usage_coverage: float = Field(0, ge=0, le=1)
    min_cache_usage_coverage: float = Field(0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_case_count(self):
        if self.task_set == "bfcl-v4-smoke-v1" and self.cases_per_category != 1:
            raise ValueError(
                "bfcl-v4-smoke-v1 has a fixed case count; use bfcl-v4-tool-use-v1 to select more"
            )
        return self


def run_process(command, *, env, deadline, cancel_event):
    """Own the entire process group, including scorer children, until reaped."""

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise BenchmarkCancelled("BFCL canceled")
        if time.monotonic() >= deadline:
            raise BenchmarkTimeout("BFCL exceeded its total wall-clock budget")

    check()
    # Upstream may print prompts and credentials in exceptions. Only our typed
    # failure and sanitized event files are published, never raw child output.
    process = subprocess.Popen(
        command,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=None if env.get("BFCL_LOCAL_DEBUG") == "1" else subprocess.DEVNULL,
    )
    try:
        while process.poll() is None:
            check()
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        check()
        if process.returncode:
            raise RuntimeError(
                f"BFCL runner exited with code {process.returncode}; inspect request events"
            )
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def _write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


class BFCLV4Module(TestModule):
    name = "bfcl_v4"
    display_name = "BFCL V4 subset"
    description = "Official BFCL frozen subset: tool-use quality and streaming performance. Not the full leaderboard."
    ParamsSchema = BFCLV4Params
    metrics_descriptors = [
        MetricDescriptor(name, name.replace("_", " "), unit, higher_is_better=higher)
        for name, unit, higher in [
            ("accuracy", "", True),
            ("accuracy_simple_python", "", True),
            ("accuracy_multi_turn_base", "", True),
            ("accuracy_multiple", "", True),
            ("accuracy_parallel", "", True),
            ("accuracy_irrelevance", "", True),
            ("ttft_p50_ms", "ms", False),
            ("ttft_p90_ms", "ms", False),
            ("ttft_p99_ms", "ms", False),
            ("request_e2e_p90_ms", "ms", False),
            ("generation_wall_seconds", "s", False),
            ("grading_wall_seconds", "s", False),
            ("request_throughput_rps", "req/s", True),
            ("successful_request_rps", "req/s", True),
            ("input_tokens_per_second", "tok/s", True),
            ("output_tokens_per_second", "tok/s", True),
            ("cache_hit_rate", "", True),
            ("cached_input_tokens", "tok", True),
            ("passed_cases_per_hour", "cases/h", True),
            ("active_cases_peak", "", True),
            ("active_http_peak", "", True),
            ("http_attempt_count", "", False),
            ("request_error_count", "", False),
            ("retry_count", "", False),
            ("infra_error_count", "", False),
            ("case_count", "", True),
            ("case_count_simple_python", "", True),
            ("case_count_multi_turn_base", "", True),
            ("case_count_multiple", "", True),
            ("case_count_parallel", "", True),
            ("case_count_irrelevance", "", True),
            ("ttft_sample_count", "", True),
            ("cache_hit_rate_first", "", True),
            ("cache_hit_rate_subsequent", "", True),
            ("cache_usage_coverage_first", "", True),
            ("cache_usage_coverage_subsequent", "", True),
            ("ttft_coverage", "", True),
            ("input_usage_coverage", "", True),
            ("output_usage_coverage", "", True),
            ("cache_usage_coverage", "", True),
        ]
    ]

    default_metric_configs = [
        MetricConfig("accuracy", role="score", formula="passthrough"),
        *[
            MetricConfig(metric.name, role="display")
            for metric in metrics_descriptors
            if metric.name != "accuracy"
        ],
    ]

    def run(
        self,
        endpoint: EndpointConfig,
        params,
        output_dir,
        progress_cb=_noop_progress,
        cancel_event=None,
    ):
        started = time.monotonic()
        deadline = started + params.max_seconds
        attempt = uuid4().hex
        root = Path(output_dir) / attempt
        root.mkdir(parents=True, exist_ok=False)
        index = root / "artifacts.json"
        config_path = root / "config.json"
        probe_root = root / "probe"
        probe_config_path = probe_root / "config.json"
        result = ModuleResult(artifacts=[str(index)])
        canceled = None
        try:
            url = urlsplit(endpoint.api_url)
            if url.username or url.password or url.query or url.fragment:
                raise ValueError(
                    "BFCL endpoint must not contain URL credentials, query or fragment"
                )
            if url.scheme not in ("http", "https") or not url.hostname:
                raise ValueError("BFCL requires an HTTP(S) endpoint")
            manifest = json.loads(MANIFESTS[params.task_set].read_text())
            if params.cases_per_category > 1:
                manifest["cases"] = {
                    category: [
                        f"{category}_{index}"
                        for index in range(params.cases_per_category)
                    ]
                    for category in manifest["cases"]
                }
            manifest.update(
                concurrency=params.concurrency,
                handler_version="llmbench-stream-v1",
                request_timeout_seconds=params.request_timeout_seconds,
                retry_policy={
                    "max_retries": params.request_retries,
                    "backoff_seconds": 0.5,
                },
                warmup={"requests": 0},
                ttft_policy="text-or-tool-calls; excludes-reasoning",
                official_maximum_step_limit=20,
            )
            if params.probe_endpoint:
                manifest["probe"] = {"case_id": "multi_turn_base_0", "concurrency": 1}
            protocol = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode()
            manifest["protocol_hash"] = hashlib.sha256(protocol).hexdigest()
            _write_json(root / "manifest.json", manifest)
            _write_json(root / "params.json", params.model_dump())
            identity = {**getattr(endpoint, "run_context", {}), "attempt_id": attempt}
            _write_json(root / "identity.json", identity)
            config = {
                "manifest": manifest,
                "endpoint": endpoint.api_url,
                "model": endpoint.model,
                "timeout": params.request_timeout_seconds,
                "concurrency": params.concurrency,
                "request_retries": params.request_retries,
                "export_trace": params.export_replay_trace,
                "identity": identity,
            }
            _write_json(config_path, config)
            config_path.chmod(0o600)
            env = {
                **os.environ,
                "BFCL_ENDPOINT_API_KEY": endpoint.api_key,
                "BFCL_PROJECT_ROOT": str(root.resolve()),
            }
            python = os.environ.get("BFCL_PYTHON", "/opt/bfcl/bin/python")
            if "/" in python and not os.access(python, os.X_OK):
                raise RuntimeError(
                    "BFCL runner environment unavailable. Use a Linux AMD64 BFCL worker or set BFCL_PYTHON."
                )
            phases = [("generate", 0.1, root), ("evaluate", 0.75, root)]
            if params.probe_endpoint:
                probe_root.mkdir()
                probe_manifest = json.loads(MANIFESTS["bfcl-v4-smoke-v1"].read_text())
                probe_manifest.update(
                    name="bfcl-v4-endpoint-probe-v1",
                    concurrency=1,
                    cases={"multi_turn_base": ["multi_turn_base_0"]},
                )
                _write_json(
                    probe_config_path,
                    {
                        **config,
                        "manifest": probe_manifest,
                        "concurrency": 1,
                        "export_trace": False,
                    },
                )
                probe_config_path.chmod(0o600)
                phases.insert(0, ("probe", 0.02, probe_root))
            for phase, fraction, phase_root in phases:
                progress_cb(fraction, f"BFCL {phase}")
                run_process(
                    [
                        python,
                        "-m",
                        "bench.bfcl.runner",
                        "--phase",
                        phase,
                        "--root",
                        str(phase_root.resolve()),
                    ],
                    env=env,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
            result.metrics = json.loads((root / "summary.json").read_text())["metrics"]
            for key in params.required_metrics:
                value = result.metrics.get(key)
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"BFCL required metric unavailable: {key}")
            for key, minimum in [
                ("ttft_coverage", params.min_ttft_coverage),
                ("input_usage_coverage", params.min_usage_coverage),
                ("output_usage_coverage", params.min_usage_coverage),
                ("cache_usage_coverage", params.min_cache_usage_coverage),
            ]:
                if minimum and (result.metrics.get(key) or 0) < minimum:
                    raise ValueError(f"BFCL telemetry coverage below threshold: {key}")
            if time.monotonic() >= deadline:
                raise BenchmarkTimeout("BFCL exceeded its total wall-clock budget")
            progress_cb(1, "BFCL complete")
        except BenchmarkCancelled as exc:
            canceled = exc
            result.error = "BFCL canceled"
        except Exception as exc:
            # Do not persist arbitrary upstream messages (may include the key).
            result.error = (
                str(exc)
                if isinstance(exc, (ValueError, BenchmarkTimeout, RuntimeError))
                else f"BFCL {type(exc).__name__}"
            )
            diagnostic = root / "runner_error.json"
            if not diagnostic.is_file():
                diagnostic = probe_root / "runner_error.json"
            if diagnostic.is_file():
                error = json.loads(diagnostic.read_text())
                result.error += f" ({error['phase']}: {error['type']})"
            if endpoint.api_key:
                result.error = result.error.replace(endpoint.api_key, "[REDACTED]")
        finally:
            config_path.unlink(missing_ok=True)
            probe_config_path.unlink(missing_ok=True)
            _write_json(
                index,
                {
                    "version": 1,
                    "attempt_id": attempt,
                    "error": result.error,
                    "files": [
                        str(p.relative_to(root))
                        for p in sorted(root.rglob("*"))
                        if p.is_file() and p != index
                    ],
                },
            )
        if canceled:
            canceled.artifacts = result.artifacts
            raise canceled
        return result
