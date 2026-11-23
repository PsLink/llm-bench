"""The real worker passes one scan to one BFCL module run and freezes effective params."""

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.api.submissions import _normalize_extra_params
from app.db.models import ModuleRunStatus, SubmissionStatus
from app.queue import jobs
from app.schemas.benchmarks import SubmissionExtraParams
from bench.modules.base import EndpointConfig


def test_worker_executes_and_freezes_the_submission_grid_without_point_fanout(tmp_path, monkeypatch):
    monkeypatch.setenv("BFCL_PYTHON", sys.executable)
    grid = [1, 3, 5, 6, 8]
    extra = _normalize_extra_params(SubmissionExtraParams(bfcl_concurrency_grid=grid))
    submission = SimpleNamespace(id=41, benchmark_id=1, extra_params=extra,
                                 status=SubmissionStatus.RUNNING, cards_per_machine=2,
                                 machine_count=1, card_type="A100")
    run = SimpleNamespace(id=42, module_name="bfcl_v4", params_json={
        "concurrency": 2, "request_timeout_seconds": 45},
        metric_configs_json=[{"key": "accuracy", "role": "redline", "min_val": 0.8}],
                          submission_id=41, status=ModuleRunStatus.PENDING)
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = run
    mod = SimpleNamespace(id=1, module_name="bfcl_v4", weight=1, order_index=0,
                          params_json={"concurrency": 2}, metric_configs_json=[
                              {"key": "accuracy", "role": "redline", "min_val": 0.99}])
    for name in ("_publish_run_event", "_persist_bfcl_artifacts", "_release_dataset_pin"):
        monkeypatch.setattr(jobs, name, lambda *args, **kwargs: None)
    monkeypatch.setattr("app.queue.log_capture.checkpoint_worker_log", lambda *args: None)
    monkeypatch.setattr(jobs, "_start_module_deadline", lambda *args: (
        threading.Event(), threading.Event(), lambda: None))
    seen = []
    def runner(command, **kwargs):
        root = Path(command[-1])
        params = json.loads((root / "config.json").read_text())
        assert params["timeout"] == 45
        if command[-3] == "evaluate":
            c = params["concurrency"]
            seen.append(c)
            (root / "summary.json").write_text(json.dumps({"metrics": {
                "total_tpm": 1000 * c, "accuracy": 0.5 if c == 8 else 0.9}}))
    monkeypatch.setattr("bench.modules.bfcl_v4.run_process", runner)
    results = []
    jobs._run_module_loop(session, submission, [mod], EndpointConfig("http://localhost/v1", "m"),
                          results, str(tmp_path), threading.Event())
    assert len(results) == 1
    assert seen == grid
    assert run.params_json["concurrency_grid"] == grid
    assert run.metrics_json["reported_concurrency"] == 6
    assert run.metrics_json["sweep_complete"] == 1
    assert run.passed and run.status == ModuleRunStatus.DONE
    assert submission.passed
    session.add.assert_not_called()


def test_changed_benchmark_is_rejected_before_running_queued_points(tmp_path):
    submission = SimpleNamespace(id=41, extra_params={"bfcl_concurrency_grid": [1, 4, 8]},
                                 benchmark=SimpleNamespace(config_hash="new"),
                                 benchmark_config_hash="frozen")
    session = MagicMock()
    results = []
    jobs._run_module_loop(session, submission, [], EndpointConfig("http://localhost/v1", "m"),
                          results, str(tmp_path), threading.Event())
    assert submission.status == SubmissionStatus.FAILED
    assert "changed" in submission.error
    assert not results
    session.query.assert_not_called()


def test_submit_rejects_changed_or_combined_scan_protocol_before_creating_rows(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from app.api import submissions
    from app.schemas.benchmarks import SubmissionCreate

    monkeypatch.setattr(submissions, "_reject_if_cordoned", AsyncMock())
    for names, expected_status in [(["bfcl_v4"], 409), (["bfcl_v4", "opencompass"], 422)]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda:
            SimpleNamespace(config_hash="new", modules=[SimpleNamespace(module_name=name)
                                                         for name in names])))
        body = SubmissionCreate(endpoint_url="http://localhost/v1", model="m", extra_params={
            "bfcl_concurrency_grid": [1, 4, 8], "expected_benchmark_config_hash": "old"})
        try:
            asyncio.run(submissions.submit_benchmark("bfcl", body, SimpleNamespace(id=1), session))
        except HTTPException as exc:
            assert exc.status_code == expected_status
        else:
            raise AssertionError("a changed or combined scan protocol must be rejected")
        session.add.assert_not_called()
