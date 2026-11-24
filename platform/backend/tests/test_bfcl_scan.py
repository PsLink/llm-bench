"""The real worker passes one scan to one BFCL module run and freezes effective params."""

import json
import sys
import threading

import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import inspect
from sqlalchemy.orm import Session, make_transient_to_detached

from app.api.submissions import _normalize_extra_params
from app.db.models import Benchmark, ModuleRunStatus, Submission, SubmissionStatus
from app.queue import jobs
from app.schemas.benchmarks import SubmissionExtraParams
from bench.modules.base import BenchmarkCancelled, BenchmarkTimeout, EndpointConfig


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("failure", [None, "timeout", "cancel", "late_cancel", "quality", "all"])
def test_worker_executes_and_freezes_the_submission_grid_without_point_fanout(
    tmp_path, monkeypatch, failure, retry,
):
    monkeypatch.setenv("BFCL_PYTHON", sys.executable)
    grid = [1, 3, 5, 6, 8]
    extra = _normalize_extra_params(SubmissionExtraParams(bfcl_concurrency_grid=grid))
    submission = SimpleNamespace(id=41, benchmark_id=1, extra_params=extra,
                                 benchmark_config_hash="frozen",
                                 status=SubmissionStatus.RUNNING, cards_per_machine=2,
                                 machine_count=1, card_type="A100", passed=None)
    frozen_configs = [
        {"key": "accuracy", "role": "redline", "min_val": 0.8},
        {"key": "total_tpm", "role": "score", "formula": "passthrough"},
        {"key": "admin_display", "role": "display"},
    ]
    run = SimpleNamespace(id=42, module_name="bfcl_v4", params_json={
        "concurrency": 2, "request_timeout_seconds": 45},
        metric_configs_json=list(frozen_configs),
        frozen_metric_configs_json=list(frozen_configs),
                          submission_id=41, status=ModuleRunStatus.PENDING)
    session = MagicMock()
    session.get.return_value = Benchmark(id=1, config_hash="frozen")
    session.query.return_value.filter_by.return_value.first.return_value = run
    if retry:
        # A previous attempt completed this module before the worker crashed.
        run.metric_configs_json.append({"key": "prior_attempt_only", "role": "display"})
        session.query.return_value.filter_by.return_value.all.return_value = [run]
        jobs._reset_submission_for_retry(session, submission)
    mod = SimpleNamespace(id=1, module_name="bfcl_v4", weight=1, order_index=0,
                          params_json={"concurrency": 2}, metric_configs_json=[
                              {"key": "accuracy", "role": "redline", "min_val": 0.99}])
    for name in ("_publish_run_event", "_publish_progress_event",
                 "_persist_bfcl_artifacts", "_release_dataset_pin"):
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
            if failure == "all":
                raise RuntimeError("point failed")
            if c == 8 and failure == "timeout":
                raise BenchmarkTimeout("point timed out")
            if c == 8 and failure == "cancel":
                submission.status = SubmissionStatus.CANCELED
                raise BenchmarkCancelled("user canceled")
            (root / "summary.json").write_text(json.dumps({"metrics": {
                "total_tpm": 1000 * c, "accuracy": 0.5 if c == 8 or failure == "quality" else 0.9}}))
    monkeypatch.setattr("bench.modules.bfcl_v4.run_process", runner)
    if failure == "late_cancel":
        refreshes = []
        def refresh(row):
            refreshes.append(row)
            if len(refreshes) == 2:
                submission.status = SubmissionStatus.CANCELED
        session.refresh.side_effect = refresh
    results = []
    jobs._run_module_loop(session, submission, [mod], EndpointConfig("http://localhost/v1", "m"),
                          results, str(tmp_path), threading.Event())
    assert seen == grid
    assert run.params_json["concurrency_grid"] == grid
    session.add.assert_not_called()
    if failure in ("cancel", "late_cancel"):
        assert run.metrics_json["sweep_canceled"] == 1
        assert run.status == ModuleRunStatus.FAILED and not run.passed
        assert not submission.passed
        assert submission.status == SubmissionStatus.CANCELED
        return
    assert len(results) == 1
    assert run.metrics_json["sweep_canceled"] == 0
    assert run.metrics_json["sweep_complete"] == int(failure not in ("timeout", "all"))
    assert run.frozen_metric_configs_json == frozen_configs
    assert run.metric_configs_json[:len(frozen_configs)] == frozen_configs
    assert "prior_attempt_only" not in {c["key"] for c in run.metric_configs_json}
    if failure in ("quality", "all"):
        assert not run.passed and not submission.passed
    else:
        assert run.metrics_json["reported_concurrency"] == 6
        assert run.passed and run.status == ModuleRunStatus.DONE
        assert submission.passed
    if failure != "all":
        expected_score = 8000 if failure == "quality" else 6000
        assert run.score == submission.score_total == expected_score
    if retry:
        # A further redelivery must also discard this attempt's display rows.
        assert any(c["key"].startswith("c1.") for c in run.metric_configs_json)
        jobs._reset_submission_for_retry(session, submission)
        assert run.metric_configs_json == run.frozen_metric_configs_json == frozen_configs


@pytest.mark.parametrize("configs", [None, [], [
    {"key": "accuracy", "role": "redline", "min_val": 0.8},
    {"key": "admin_display", "role": "display"},
]])
def test_retry_preserves_legacy_bfcl_snapshot_and_missing_config_fallback(configs):
    from app.db.models import SubmissionRun

    run = SubmissionRun(module_name="bfcl_v4", metric_configs_json=configs)
    submission = Submission(id=41)
    session = MagicMock()
    session.query.return_value.filter_by.return_value.all.return_value = [run]
    jobs._reset_submission_for_retry(session, submission)
    assert run.metric_configs_json == configs
    assert run.frozen_metric_configs_json == configs
    if configs:
        run.metric_configs_json.append({"key": "runtime_only", "role": "display"})
        jobs._reset_submission_for_retry(session, submission)
        assert run.metric_configs_json == configs


def test_submit_freezes_bfcl_rules_separately_from_display_snapshot(monkeypatch):
    import asyncio
    from datetime import datetime
    from unittest.mock import AsyncMock

    from app.api import submissions
    from app.db.models import SubmissionRun
    from app.schemas.benchmarks import SubmissionCreate

    configs = [{"key": "accuracy", "role": "redline", "min_val": 0.8}]
    mod = SimpleNamespace(id=1, module_name="bfcl_v4", params_json={}, metric_configs_json=configs)
    benchmark = SimpleNamespace(id=1, config_hash="frozen", modules=[mod])
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: benchmark))
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    added = []
    session.add.side_effect = added.append

    async def flush():
        added[0].id = 41
        added[0].created_at = datetime.now()

    session.flush = AsyncMock(side_effect=flush)
    monkeypatch.setattr(submissions, "_reject_if_cordoned", AsyncMock())
    monkeypatch.setattr(submissions, "_enforce_submission_quota", AsyncMock())
    monkeypatch.setattr(jobs, "enqueue_submission", lambda *args: None)
    body = SubmissionCreate(endpoint_url="http://localhost/v1", model="m")
    asyncio.run(submissions.submit_benchmark("bfcl", body, SimpleNamespace(id=1), session))
    run = next(row for row in added if isinstance(row, SubmissionRun))
    assert run.metric_configs_json == run.frozen_metric_configs_json == configs
    run.metric_configs_json.append({"key": "runtime_only", "role": "display"})
    assert run.frozen_metric_configs_json == configs[:1]


def test_changed_benchmark_is_rejected_before_running_queued_points(tmp_path):
    submission = SimpleNamespace(id=41, benchmark_id=1,
                                 extra_params={"bfcl_concurrency_grid": [1, 4, 8]},
                                 benchmark_config_hash="frozen")
    session = MagicMock()
    session.get.return_value = Benchmark(id=1, config_hash="new")
    results = []
    jobs._run_module_loop(session, submission, [], EndpointConfig("http://localhost/v1", "m"),
                          results, str(tmp_path), threading.Event())
    assert submission.status == SubmissionStatus.FAILED
    assert "changed" in submission.error
    assert not results
    session.query.assert_not_called()


@pytest.mark.parametrize("benchmark_id, grid_source, current_hash", [
    (None, None, None),
    (1, None, None),
    (None, "submission", None),
    (1, "submission", "frozen"),
    (1, "module", "frozen"),
    (1, "submission", "changed"),
    (1, "submission", None),
])
def test_worker_handles_unloaded_benchmark_relationship(
    tmp_path, benchmark_id, grid_source, current_hash,
):
    # Restore the ORM state returned by a fresh worker query, without needing
    # database services. A namespace cannot reproduce the lazy='raise' guard.
    submission = Submission(
        id=41, benchmark_id=benchmark_id, benchmark_config_hash="frozen",
        extra_params={"bfcl_concurrency_grid": [1, 4]} if grid_source == "submission" else {},
        status=SubmissionStatus.CANCELED,
    )
    make_transient_to_detached(submission)
    session = MagicMock()
    session.get.return_value = (
        Benchmark(id=1, config_hash=current_hash) if current_hash is not None else None
    )
    modules = [SimpleNamespace(params_json={"concurrency_grid": [1, 4]})] if grid_source == "module" else []
    with Session() as orm_session:
        orm_session.add(submission)
        assert "benchmark" in inspect(submission).unloaded
        jobs._run_module_loop(
            session, submission, modules, EndpointConfig("http://localhost/v1", "m"),
            [], str(tmp_path), threading.Event(),
        )
        assert "benchmark" in inspect(submission).unloaded
    if benchmark_id is not None and grid_source is not None:
        session.get.assert_called_once_with(Benchmark, benchmark_id)
        if current_hash != "frozen":
            assert submission.status == SubmissionStatus.FAILED
            assert "benchmark" in submission.error.lower()
        else:
            assert submission.status == SubmissionStatus.CANCELED
    else:
        session.get.assert_not_called()
        assert submission.status == SubmissionStatus.CANCELED


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
