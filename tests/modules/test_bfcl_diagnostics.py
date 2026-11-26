import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from bench.bfcl import runner
from bench.modules.base import BenchmarkCancelled, BenchmarkTimeout, EndpointConfig
from bench.modules.bfcl_v4 import BFCLV4Module, BFCLV4Params


def test_runner_preserves_safe_failure_location(tmp_path):
    with patch.object(sys, "argv", ["runner", "--phase", "generate", "--root", str(tmp_path)]), patch.object(
        runner, "run_phase", side_effect=ValueError("secret prompt credential")
    ), pytest.raises(ValueError):
        runner.main()
    diagnostic = json.loads((tmp_path / "runner_error.json").read_text())
    assert diagnostic["type"] == "ValueError"
    assert diagnostic["frames"]
    assert "secret" not in json.dumps(diagnostic)


@pytest.mark.parametrize("failure", [RuntimeError("child failed"), BenchmarkCancelled("canceled"),
                                     BenchmarkTimeout("budget exceeded")])
def test_attempt_failure_snapshot_survives_bad_diagnostic(tmp_path, monkeypatch, caplog, failure):
    monkeypatch.setenv("BFCL_PYTHON", sys.executable)
    def process(command, **kwargs):
        root = Path(command[-1])
        (root / "runner_error.json").write_text('{"unfinished":')
        (root / "request_events.jsonl").write_text(json.dumps({
            "case_id": "multi_turn_base_0", "request_index": 1,
            "status": "error", "error_type": "ValueError", "http_status": 200,
            "body": "private prompt", "message": "secret",
        }) + '\n{"partial":')
        raise failure
    with patch("bench.modules.bfcl_v4.run_process", side_effect=process), caplog.at_level("INFO"):
        try:
            BFCLV4Module().run(EndpointConfig("http://localhost/v1", "m"), BFCLV4Params(), tmp_path)
        except BenchmarkCancelled:
            pass
    state = json.loads(next(tmp_path.glob("*/state.json")).read_text())
    assert state["status"] == ("canceled" if isinstance(failure, BenchmarkCancelled) else
                               "timed_out" if isinstance(failure, BenchmarkTimeout) else "failed")
    assert state["phase"] == "generate"
    assert state["requests"]["failed"] == 1
    assert "ValueError" in caplog.text
    assert "private prompt" not in caplog.text
    assert "secret" not in caplog.text
    assert "diagnostic_unreadable" in caplog.text


def test_failed_point_is_checkpointed_before_next_point(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("BFCL_PYTHON", sys.executable)
    checkpoints = []
    def progress(*args):
        pass
    progress.checkpoint = lambda: checkpoints.append(True)
    def process(command, **kwargs):
        root = Path(command[-1])
        config = json.loads((root / "config.json").read_text())
        if config["concurrency"] == 1:
            (root / "runner_error.json").write_text('{"phase":"generate","type":"ValueError"}')
            raise RuntimeError("BFCL runner exited with code 1; inspect request events")
        scan_root = root.parent.parent
        saved = json.loads((scan_root / "scan_state.json").read_text())
        assert saved["points"]["c1"]["status"] == "failed"
        assert checkpoints
        if command[-3] == "evaluate":
            (root / "summary.json").write_text('{"metrics":{"accuracy":1,"total_tpm":100}}')
    with patch("bench.modules.bfcl_v4.run_process", side_effect=process), caplog.at_level("INFO"):
        result = BFCLV4Module().run(EndpointConfig("http://localhost/v1", "m"),
                                  BFCLV4Params(concurrency_grid=[1, 2]), tmp_path, progress)
    assert result.error is None
    assert result.metrics["c1"]["valid"] == 0
    assert result.metrics["c1"]["diagnostic"]["diagnostic"]["type"] == "ValueError"
    assert "ValueError" in caplog.text
    saved = json.loads(next(tmp_path.glob("*/scan_state.json")).read_text())
    assert saved["points"]["c1"]["status"] == "failed"
    assert saved["points"]["c2"]["status"] == "completed"


def test_event_summary_reads_appended_records_once_and_waits_for_partial_line(tmp_path):
    from bench.bfcl.diagnostics import EventSummary
    path = tmp_path / "request_events.jsonl"
    path.write_text('{"status":"success"}\n{"status":')
    summary = EventSummary(tmp_path)
    assert summary.poll()["total"] == 1
    assert summary.poll()["total"] == 1
    with path.open("a") as output:
        output.write('"error","error_type":"ValueError"}\n')
    assert summary.poll()["total"] == 2
    assert summary.poll()["failed"] == 1
    assert summary.poll()["error_types"] == {"ValueError": 1}


def test_generation_validation_reports_case_ids_without_upstream_error(tmp_path):
    from bench.bfcl.diagnostics import exception_details
    from bench.bfcl.smoke import check_generated_cases
    path = tmp_path / "BFCL_v4_multi_turn_base_result.json"
    path.write_text(json.dumps({"id": "multi_turn_base_0", "error": "private prompt secret"}))
    with pytest.raises(ValueError) as caught:
        check_generated_cases(tmp_path, {"multi_turn_base": ["multi_turn_base_0"]})
    diagnostic = exception_details(caught.value)
    assert diagnostic["code"] == "generation_inference_failed"
    assert diagnostic["details"]["failed_case_ids"] == ["multi_turn_base_0"]
    assert "secret" not in json.dumps(diagnostic)


def test_actual_child_failure_is_logged_and_exit_status_saved(tmp_path, caplog):
    import time

    from bench.modules.bfcl_v4 import run_process
    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="code 1"):
        run_process([sys.executable, "-c", "raise ValueError('private prompt')",
                     "--phase", "generate", "--root", str(tmp_path)],
                    env={}, deadline=time.monotonic() + 10, cancel_event=None)
    assert json.loads((tmp_path / "process_state.json").read_text())["exit_code"] == 1
    assert "exit_code=1" in caplog.text
    assert "private prompt" not in caplog.text



def test_summary_reports_event_age_even_on_first_read(tmp_path):
    from bench.bfcl.diagnostics import EventSummary

    (tmp_path / "request_events.jsonl").write_text(
        '{"status":"success","finished_monotonic":10}\n')
    with patch("bench.bfcl.diagnostics.time.monotonic", return_value=100):
        assert EventSummary(tmp_path).poll()["seconds_since_last_event"] == 90


def test_runner_emits_progress_heartbeat(tmp_path, caplog):
    from types import SimpleNamespace

    from bench.modules.bfcl_v4 import run_process

    (tmp_path / "request_events.jsonl").write_text('{"status":"success"}\n')
    polls = iter([None, None, 0, 0])
    child = SimpleNamespace(pid=123, returncode=0, poll=lambda: next(polls))
    ticks = iter(range(0, 1000, 10))
    with patch("bench.modules.bfcl_v4.subprocess.Popen", return_value=child), patch(
        "bench.modules.bfcl_v4.time.monotonic", side_effect=lambda: next(ticks)
    ), patch("bench.modules.bfcl_v4.time.sleep"), caplog.at_level("INFO"):
        run_process([sys.executable, "--phase", "generate", "--root", str(tmp_path)],
                    env={}, deadline=9999, cancel_event=None)
    assert "runner heartbeat phase=generate" in caplog.text
    assert '"succeeded": 1' in caplog.text


def test_process_root_need_not_be_last_argument(tmp_path):
    import time
    from bench.modules.bfcl_v4 import run_process

    run_process([sys.executable, "-c", "pass", "--root", str(tmp_path), "--phase", "generate"],
                env={}, deadline=time.monotonic() + 10, cancel_event=None)
    assert json.loads((tmp_path / "process_state.json").read_text())["phase"] == "generate"


def test_start_log_failure_still_reaps_child(tmp_path):
    import time
    from unittest.mock import Mock
    from bench.modules.bfcl_v4 import run_process

    child = Mock(pid=123, returncode=-15)
    child.poll.return_value = None
    with patch("bench.modules.bfcl_v4.subprocess.Popen", return_value=child), patch(
        "bench.modules.bfcl_v4.os.killpg"
    ) as kill, patch("bench.modules.bfcl_v4.logger.info", side_effect=RuntimeError("log failed")):
        with pytest.raises(RuntimeError, match="log failed"):
            run_process([sys.executable, "--root", str(tmp_path)], env={},
                        deadline=time.monotonic() + 10, cancel_event=None)
    kill.assert_called_once()
    child.wait.assert_called_once()


@pytest.mark.parametrize("failure", [BenchmarkTimeout("budget"), BenchmarkCancelled("cancel"),
                                     RuntimeError("BFCL runner exited with code -9")])
def test_terminated_runner_state_survives_without_error_file(tmp_path, monkeypatch, failure):
    from bench.bfcl.diagnostics import read_error

    monkeypatch.setenv("BFCL_PYTHON", sys.executable)

    def process(command, **kwargs):
        root = Path(command[-1])
        (root / "runner_state.json").write_text('{"phase":"generate","stage":"validate_generated_cases"}')
        (root / "process_state.json").write_text('{"phase":"generate","pid":123,"exit_code":-9}')
        raise failure

    with patch("bench.modules.bfcl_v4.run_process", side_effect=process), patch(
        "bench.modules.bfcl_v4.read_error", wraps=read_error
    ) as read:
        try:
            result = BFCLV4Module().run(EndpointConfig("http://localhost/v1", "m"), BFCLV4Params(), tmp_path)
        except BenchmarkCancelled as exc:
            result = exc.partial_result
    read.assert_called_once()
    snapshot = result.metrics["diagnostic"]
    assert snapshot["runner_state"]["stage"] == "validate_generated_cases"
    assert snapshot["process_state"]["exit_code"] == -9
    assert snapshot["process_state"]["pid"] == 123


@pytest.mark.parametrize("phase", ["setup", "validate_metrics"])
def test_parent_failure_does_not_claim_missing_runner_error(tmp_path, monkeypatch, phase):
    monkeypatch.setenv("BFCL_PYTHON", sys.executable)

    def process(command, **kwargs):
        if command[-3] == "evaluate":
            (Path(command[-1]) / "summary.json").write_text('{"metrics":{}}')

    endpoint = EndpointConfig("ftp://localhost" if phase == "setup" else "http://localhost/v1", "m")
    with patch("bench.modules.bfcl_v4.run_process", side_effect=process), patch(
        "bench.modules.bfcl_v4.read_error"
    ) as read:
        result = BFCLV4Module().run(endpoint, BFCLV4Params(), tmp_path)
    snapshot = result.metrics["diagnostic"]
    assert snapshot["phase"] == phase
    assert snapshot["diagnostic"]["code"] == "parent_failure"
    assert snapshot["diagnostic"]["type"] == "ValueError"
    read.assert_not_called()


def test_case_and_request_samples_share_bounded_filtering(tmp_path):
    from bench.bfcl.diagnostics import EventSummary

    frame = {"file": "runner.py", "function": "inference", "line": 10, "source": "secret"}
    row = {"status": "error", "case_id": "x" * 200, "request_index": True,
           "phase": "generate", "type": {"message": "secret"}, "frames": [frame] * 20}
    for name in ("request_events.jsonl", "case_events.jsonl"):
        (tmp_path / name).write_text(json.dumps(row) + "\n")
    result = EventSummary(tmp_path).poll()
    for key in ("failure_samples", "case_failure_samples"):
        sample = result[key][0]
        assert "case_id" not in sample
        assert "type" not in sample
        assert "request_index" not in sample
        assert len(sample["frames"]) == 12
        assert "secret" not in json.dumps(sample)
    assert result["failure_samples"][0]["phase"] == "generate"


def test_scan_checkpoints_do_not_repeat_full_point_diagnostics(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("BFCL_PYTHON", sys.executable)

    def process(command, **kwargs):
        root = Path(command[-1])
        config = json.loads((root / "config.json").read_text())
        if config["concurrency"] == 1:
            (root / "runner_error.json").write_text(json.dumps({
                "phase": "generate", "type": "ValueError", "frames": [{"file": "runner.py"}],
            }))
            raise RuntimeError("child failed")
        if command[-3] == "evaluate":
            (root / "summary.json").write_text('{"metrics":{"accuracy":1,"total_tpm":10}}')

    with patch("bench.modules.bfcl_v4.run_process", side_effect=process), caplog.at_level("INFO"):
        result = BFCLV4Module().run(EndpointConfig("http://localhost/v1", "m"),
                                  BFCLV4Params(concurrency_grid=[1, 2]), tmp_path)
    checkpoints = [r.message for r in caplog.records if "BFCL scan checkpoint" in r.message]
    assert checkpoints
    assert all("frames" not in message and "diagnostic" not in message for message in checkpoints)
    saved = json.loads(next(tmp_path.glob("*/scan_state.json")).read_text())
    assert saved["points"]["c1"]["diagnostic"] == result.metrics["c1"]["diagnostic"]


@pytest.mark.parametrize("state", ['{"unfinished":', '[]', 'x' * 65537,
                                   '{"phase":"probe","stage":"old_stage"}'])
def test_bad_runner_states_do_not_replace_original_failure(tmp_path, monkeypatch, state):
    monkeypatch.setenv("BFCL_PYTHON", sys.executable)

    def process(command, **kwargs):
        root = Path(command[-1])
        for name in ("runner_state.json", "process_state.json"):
            (root / name).write_text(state)
        raise RuntimeError("original child failure")

    with patch("bench.modules.bfcl_v4.run_process", side_effect=process):
        result = BFCLV4Module().run(EndpointConfig("http://localhost/v1", "m"), BFCLV4Params(), tmp_path)
    assert result.error == "original child failure"
    snapshot = result.metrics["diagnostic"]
    assert snapshot["runner_state"] == {"code": "diagnostic_unreadable"}
    assert snapshot["process_state"] == {"code": "diagnostic_unreadable"}


@pytest.mark.parametrize("grid", [None, [1, 2]])
@pytest.mark.parametrize("failure,status", [(RuntimeError("child failed"), "failed"),
                                           (BenchmarkTimeout("budget"), "timed_out"),
                                           (BenchmarkCancelled("cancel"), "canceled")])
def test_tail_selection_failure_preserves_runner_diagnostics(tmp_path, monkeypatch, grid, failure, status):
    monkeypatch.setenv("BFCL_PYTHON", sys.executable)

    def process(command, **kwargs):
        assert command[-3] == "select"
        root = Path(command[-1])
        (root / "runner_state.json").write_text('{"phase":"select","stage":"setup"}')
        (root / "process_state.json").write_text('{"phase":"select","pid":123,"exit_code":-15}')
        if status == "failed":
            (root / "runner_error.json").write_text(json.dumps({
                "phase": "select", "type": "DiagnosticError", "code": "installed_revision_mismatch",
            }))
        raise failure

    params = BFCLV4Params(task_set="bfcl-v4-multi-turn-long-context-v1", case_selection="tail",
                          concurrency_grid=grid)
    with patch("bench.modules.bfcl_v4.run_process", side_effect=process):
        try:
            result = BFCLV4Module().run(EndpointConfig("http://localhost/v1", "m"), params, tmp_path)
        except BenchmarkCancelled as exc:
            result = exc.partial_result
    snapshot = (result.metrics["c1"] if grid else result.metrics)["diagnostic"]
    assert snapshot["status"] == status
    assert snapshot["phase"] == "select"
    assert snapshot["runner_state"]["stage"] == "setup"
    assert snapshot["process_state"]["exit_code"] == -15
    assert snapshot["process_state"]["pid"] == 123
    assert snapshot["diagnostic"]["code"] == (
        "installed_revision_mismatch" if status == "failed" else "diagnostic_missing")


def test_invalid_selection_output_is_a_parent_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BFCL_PYTHON", sys.executable)

    def process(command, **kwargs):
        assert command[-3] == "select"
        (Path(command[-1]) / "case-selection.json").write_text('{"unfinished":')

    with patch("bench.modules.bfcl_v4.run_process", side_effect=process):
        result = BFCLV4Module().run(EndpointConfig("http://localhost/v1", "m"), BFCLV4Params(
            task_set="bfcl-v4-multi-turn-long-context-v1", case_selection="tail"), tmp_path)
    assert result.metrics["diagnostic"]["diagnostic"]["code"] == "parent_failure"
    assert result.metrics["diagnostic"]["diagnostic"]["type"] == "JSONDecodeError"
