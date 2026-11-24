"""Cancellation verdicts shared by the API and worker finalization.

Callers hold the submission row lock until these changes are committed. This
serializes cancellation with worker completion without locking during a run.
"""
from __future__ import annotations

from datetime import datetime


def canceled_scan_metrics(metrics: dict | None) -> dict:
    return {
        **(metrics or {}),
        "sweep_canceled": 1,
        "sweep_complete": 0,
        "sweep_stop_reason": "BFCL scan canceled",
    }


def apply_cancellation(submission, runs) -> list:
    """Invalidate the submission and its scans, preserving diagnostic metrics."""
    from app.db.models import ModuleRunStatus

    submission.passed = False
    submission.score_total = 0.0
    scans = []
    for run in runs:
        if run.module_name != "bfcl_v4" or not (
            (submission.extra_params or {}).get("bfcl_concurrency_grid")
            or (run.params_json or {}).get("concurrency_grid")
            or "sweep_canceled" in (run.metrics_json or {})
        ):
            continue
        run.status = ModuleRunStatus.FAILED
        run.passed = False
        run.score = 0.0
        run.error = "BFCL scan canceled"
        run.metrics_json = canceled_scan_metrics(run.metrics_json)
        run.finished_at = datetime.utcnow()
        scans.append(run)
    return scans
