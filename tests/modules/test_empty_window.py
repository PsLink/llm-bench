"""Levels whose measured window came out empty (issue #34).

A count-limited level against a slow endpoint can be stopped by its duration
cap with every finished request inside the warmup share. guidellm then reports
0 for every statistic and a negative duration. These tests pin what happens
instead: the level is rebuilt from the requests it finished (flagged as a thin
sample), or, when it can't be, reported as empty with no values — never as a
measured 0 — and the auto search never climbs on such a level.
"""
import copy

import pytest

from bench.modules import get_module
from bench.modules.base import EndpointConfig
import bench.modules.perf_guidellm as perf_mod
import bench.modules.perf_guidellm_sweep as sweep_mod
from bench.modules.perf_guidellm_sweep import PerfGuidellmSweepParams, _ProbeOutcome
from bench.tests.b_realworld import RealWorldTest
from bench.tests.base import _nearest_rank, rebuild_empty_windows

_ENDPOINT = EndpointConfig(api_url="http://localhost:9", model="m", api_key="")


def _zero_stat():
    return {"successful": {"mean": 0.0, "median": 0.0,
                           "percentiles": {"p50": 0.0, "p90": 0.0, "p99": 0.0}}}


def _request(start, ttft_ms, latency_s, prompt=50_000, output=1_500):
    return {
        "request_start_time": start,
        "request_end_time": start + latency_s,
        "request_latency": latency_s,
        "prompt_tokens": prompt,
        "output_tokens": output,
        "total_tokens": prompt + output,
        "time_to_first_token_ms": ttft_ms,
        "time_per_output_token_ms": 1000.0 * latency_s / output,
        "inter_token_latency_ms": (1000.0 * latency_s - ttft_ms) / (output - 1),
    }


def _empty_level(conc=1, successful=None, incomplete=1, made=None):
    """The shape from the issue: request_totals all zero, a negative duration,
    zero placeholders everywhere, and the finished requests still stored."""
    if successful is None:
        successful = [_request(1000.0, 35_400.0, 380.0),
                      _request(1380.5, 42_600.0, 172.0)]
    stored = {"successful": successful, "errored": [],
              "incomplete": [{"request_start_time": 1552.5}] * incomplete}
    if made is None:
        made = {s: len(v) for s, v in stored.items()}
        made["total"] = sum(made.values())
    metrics = {name: _zero_stat() for name in (
        "prompt_tokens_per_second", "output_tokens_per_second",
        "tokens_per_second", "requests_per_second", "time_to_first_token_ms",
        "time_per_output_token_ms", "inter_token_latency_ms", "request_latency")}
    metrics["request_totals"] = {"successful": 0, "errored": 0,
                                 "incomplete": 0, "total": 0}
    return {
        "config": {"strategy": {"max_concurrency": conc}},
        "metrics": metrics,
        "requests": stored,
        "scheduler_metrics": {"requests_made": made},
        "start_time": 999.0,
        "end_time": 1600.0,
        "duration": -138.9,
    }


def _measured_level(conc=1):
    def stat(v):
        return {"successful": {"mean": v, "percentiles": {"p50": v, "p90": v, "p99": v}}}
    return {
        "config": {"strategy": {"max_concurrency": conc}},
        "metrics": {
            "prompt_tokens_per_second": stat(1000.0),
            "output_tokens_per_second": stat(100.0),
            "tokens_per_second": stat(1100.0),
            "time_to_first_token_ms": stat(200.0),
            "time_per_output_token_ms": stat(20.0),
            "inter_token_latency_ms": stat(20.0),
            "request_totals": {"successful": 8, "errored": 0, "incomplete": 0, "total": 8},
        },
        "requests": {"successful": [_request(1.0, 1.0, 1.0)], "errored": [], "incomplete": []},
        "scheduler_metrics": {"requests_made": {"successful": 10, "errored": 0,
                                                "incomplete": 0, "total": 10}},
        "start_time": 0.0, "end_time": 50.0, "duration": 40.0,
    }


def _extract(results):
    return RealWorldTest("http://localhost:9", "m", "", "/tmp/out",
                         rates=[1.0])._extract_metrics(results)


# ---------------------------------------------------------------------------
# Rebuild (bench.tests.base.rebuild_empty_windows)
# ---------------------------------------------------------------------------

def test_nearest_rank_never_interpolates():
    values = [10.0, 20.0, 30.0, 40.0]
    assert _nearest_rank(values, 50.0) == 20.0
    assert _nearest_rank(values, 90.0) == 40.0
    assert _nearest_rank(values, 0.1) == 10.0
    assert _nearest_rank([7.0], 99.0) == 7.0


def test_empty_level_is_rebuilt_from_the_requests_it_finished():
    results = {"benchmarks": [_empty_level()]}
    assert rebuild_empty_windows(results, "t") == [0]
    m = _extract(results)

    assert m["warmup_included_c1"] == 2.0
    assert m["measured_requests_c1"] == 3.0        # 2 successful + 1 incomplete
    assert m["uptime_c1"] == 1.0                   # incomplete is not an error
    assert m["ttft_mean_ms_c1"] == pytest.approx(39_000.0)
    assert m["ttft_p90_ms_c1"] == 42_600.0          # nearest rank: the slower one
    assert m["ttft_p50_ms_c1"] == 35_400.0
    span = (1380.5 + 172.0) - 1000.0
    assert m["duration_seconds_c1"] == pytest.approx(span)
    assert m["input_tps_mean_c1"] == pytest.approx(100_000 / span)
    assert m["output_tps_mean_c1"] == pytest.approx(3_000 / span)
    assert m["total_tps_mean_c1"] == pytest.approx(103_000 / span)
    assert m["output_tps_p99_c1"] == m["output_tps_mean_c1"]   # one aggregate
    assert "empty_window_c1" not in m


def test_levels_that_measured_anything_are_untouched():
    level = _measured_level()
    results = {"benchmarks": [copy.deepcopy(level)]}
    assert rebuild_empty_windows(results, "t") == []
    assert results["benchmarks"][0] == level
    assert "warmup_included_c1" not in _extract(results)


def test_window_holding_only_the_cut_off_request_is_rebuilt():
    """Seen on a real guidellm 0.7.4 run: both finished requests fell in
    warmup, and the measured window held only the request the cap cut off —
    request_totals.total == 1, but every statistic a 0 placeholder."""
    level = _empty_level()
    level["metrics"]["request_totals"] = {"successful": 0, "errored": 0,
                                          "incomplete": 1, "total": 1}
    results = {"benchmarks": [level]}
    assert rebuild_empty_windows(results, "t") == [0]
    m = _extract(results)
    assert m["warmup_included_c1"] == 2.0
    assert m["ttft_mean_ms_c1"] == pytest.approx(39_000.0)


def test_window_with_only_errors_keeps_its_uptime_but_no_values():
    level = _empty_level(successful=[], incomplete=0)
    level["metrics"]["request_totals"] = {"successful": 0, "errored": 2,
                                          "incomplete": 0, "total": 2}
    m = _extract({"benchmarks": [level]})
    assert m["empty_window_c1"] == 1.0
    assert m["uptime_c1"] == 0.0
    assert "ttft_p99_ms_c1" not in m


def test_no_rebuild_when_the_stored_requests_are_only_a_sample():
    """guidellm stores at most sample_size requests per status: when more
    finished than were stored, the stored ones are an arbitrary subset."""
    made = {"successful": 12, "errored": 0, "incomplete": 1, "total": 13}
    results = {"benchmarks": [_empty_level(made=made)]}
    assert rebuild_empty_windows(results, "t") == []
    m = _extract(results)
    assert m["empty_window_c1"] == 1.0


def test_incomplete_requests_that_never_started_do_not_block_a_rebuild():
    """requests_made.incomplete also counts requests cancelled before they
    started, which guidellm does not store (seen on a real 0.7.4 report:
    2 made, 1 stored)."""
    made = {"successful": 2, "errored": 0, "incomplete": 2, "total": 4}
    results = {"benchmarks": [_empty_level(made=made)]}
    assert rebuild_empty_windows(results, "t") == [0]


def test_no_rebuild_when_nothing_succeeded():
    results = {"benchmarks": [_empty_level(successful=[], incomplete=1)]}
    assert rebuild_empty_windows(results, "t") == []
    assert _extract(results)["empty_window_c1"] == 1.0


# ---------------------------------------------------------------------------
# Extraction: an empty window has no values, not zeros
# ---------------------------------------------------------------------------

def test_empty_window_reports_no_values():
    m = _extract({"benchmarks": [_empty_level(successful=[], incomplete=1)]})
    assert m["empty_window_c1"] == 1.0
    assert m["measured_requests_c1"] == 0.0
    assert m["run_seconds_c1"] == pytest.approx(601.0)
    for key in ("ttft_p99_ms_c1", "ttft_mean_ms_c1", "output_tps_mean_c1",
                "total_tps_mean_c1", "uptime_c1", "duration_seconds_c1"):
        assert key not in m, key


def test_measured_level_reports_run_seconds_and_no_flags():
    m = _extract({"benchmarks": [_measured_level()]})
    assert m["run_seconds_c1"] == 50.0
    assert m["duration_seconds_c1"] == 40.0
    assert "empty_window_c1" not in m and "warmup_included_c1" not in m


# ---------------------------------------------------------------------------
# perf_guidellm: an empty level is an error, not a 0 score
# ---------------------------------------------------------------------------

def test_single_level_module_errors_on_an_empty_window(monkeypatch, tmp_path):
    monkeypatch.setattr(perf_mod, "run_guidellm_load", lambda *a, **k: {
        "measured_requests_c1": 0.0, "empty_window_c1": 1.0, "run_seconds_c1": 600.0})
    cls = get_module("perf_guidellm")
    result = cls().run(_ENDPOINT, cls.ParamsSchema(concurrency=1), str(tmp_path))
    assert result.error and "measured no requests" in result.error


# ---------------------------------------------------------------------------
# Sweep, grid mode
# ---------------------------------------------------------------------------

def _flat_level(level, ttft=100.0, out_tps=50.0, measured=20.0, run_seconds=10.0, **extra):
    s = f"_c{level}"
    m = {f"ttft_p99_ms{s}": ttft, f"ttft_mean_ms{s}": ttft,
         f"output_tps_mean{s}": out_tps * level, f"input_tps_mean{s}": 100.0 * level,
         f"total_tps_mean{s}": (out_tps + 100.0) * level, f"uptime{s}": 1.0,
         f"measured_requests{s}": measured, f"run_seconds{s}": run_seconds}
    m.update({f"{k}{s}": v for k, v in extra.items()})
    return m


def _empty_flat(level, run_seconds=300.0):
    s = f"_c{level}"
    return {f"measured_requests{s}": 0.0, f"empty_window{s}": 1.0,
            f"run_seconds{s}": run_seconds}


def _run_grid(monkeypatch, tmp_path, flat, **params):
    monkeypatch.setattr(sweep_mod, "run_guidellm_load", lambda *a, **k: dict(flat))
    p = PerfGuidellmSweepParams(**params)
    return get_module("perf_guidellm_sweep")().run(_ENDPOINT, p, str(tmp_path))


def test_grid_never_reports_an_empty_level(monkeypatch, tmp_path):
    flat = {**_flat_level(1), **_empty_flat(2)}
    result = _run_grid(monkeypatch, tmp_path, flat, concurrencies="1,2",
                       report_level="peak")
    assert result.error is None
    assert result.metrics["reported_concurrency"] == 1
    assert result.metrics["c2"]["empty_window"] == 1.0
    assert result.metrics["c2"]["meets_slo"] == 0.0
    assert result.metrics["peak_concurrency"] == 1


def test_grid_errors_when_every_level_is_empty(monkeypatch, tmp_path):
    flat = {**_empty_flat(1), **_empty_flat(2)}
    result = _run_grid(monkeypatch, tmp_path, flat, concurrencies="1,2")
    assert result.error and "measured no requests" in result.error
    assert "c1" in result.metrics          # the bookkeeping is still kept


def test_grid_stamps_truncated_levels(monkeypatch, tmp_path):
    # c2 asked for 2 x 20 requests, measured 5, and ran its whole 300s cap.
    flat = {**_flat_level(1), **_flat_level(2, measured=5.0, run_seconds=300.0)}
    result = _run_grid(monkeypatch, tmp_path, flat, concurrencies="1,2",
                       report_level="fixed", fixed_level=2)
    m = result.metrics
    assert m["c1"]["truncated"] == 0.0
    assert m["c2"]["truncated"] == 1.0
    assert m["reported_level_truncated"] == 1.0
    assert m["reported_level_warmup_included"] == 0.0
    assert "truncated" not in m             # bookkeeping stays per level


def test_grid_promotes_the_warmup_included_flag(monkeypatch, tmp_path):
    flat = _flat_level(1, measured=3.0, warmup_included=2.0)
    result = _run_grid(monkeypatch, tmp_path, flat, concurrencies="1")
    assert result.metrics["reported_level_warmup_included"] == 2.0


# ---------------------------------------------------------------------------
# Sweep, auto mode
# ---------------------------------------------------------------------------

def _auto_params(**kw):
    kw.setdefault("search_mode", "auto")
    kw.setdefault("warmup_seconds", 0.0)
    kw.setdefault("probe_seconds", 5.0)
    kw.setdefault("max_seconds", 6.0)
    kw.setdefault("search_max_concurrency", 16)
    kw.setdefault("search_granularity", 1)
    kw.setdefault("level_settle_seconds", 0.0)
    kw.setdefault("level_drain_max_seconds", 0.0)
    return PerfGuidellmSweepParams(**kw)


def _fake_probe(levels):
    """levels(level, is_confirm) -> level_metrics dict (or None = passing)."""
    calls = []

    def fake(endpoint, params, level, duration, probe_dir, tick, cancel_event,
             log_tag="", requests_per_concurrency=None, random_seed=None, **_):
        is_confirm = duration == params.max_seconds
        calls.append((level, "confirm" if is_confirm else "probe"))
        lm = levels(level, is_confirm)
        if lm is None:
            lm = {"output_tps_mean": 50.0 * level, "total_tps_mean": 150.0 * level,
                  "ttft_p99_ms": 100.0, "uptime": 1.0,
                  "measured_requests": float(5 * level)}
        lm = dict(lm)
        if "output_tps_mean" in lm:
            lm["request_output_tps"] = lm["output_tps_mean"] / level
        return _ProbeOutcome(level=level, duration_seconds=duration, level_metrics=lm)

    fake.calls = calls
    return fake


def _run_auto(monkeypatch, tmp_path, p, fake):
    monkeypatch.setattr(sweep_mod, "_run_probe", fake)
    return get_module("perf_guidellm_sweep")().run(_ENDPOINT, p, str(tmp_path))


_EMPTY = {"measured_requests": 0.0, "empty_window": 1.0}


def test_auto_empty_levels_never_pass_even_with_a_zero_tps_floor(monkeypatch, tmp_path):
    """With slo_min_request_output_tps=0, guidellm's zero placeholders used to
    pass (0 ms TTFT is under any cap) and the ramp climbed on no data."""
    p = _auto_params(slo_min_request_output_tps=0.0)
    fake = _fake_probe(lambda level, confirm: _EMPTY)
    result = _run_auto(monkeypatch, tmp_path, p, fake)
    assert fake.calls == [(1, "probe"), (1, "confirm")]
    assert result.error and "measured no requests" in result.error
    assert result.metrics["c1"]["inconclusive"] == 1.0
    assert "reported_concurrency" not in result.metrics


def test_auto_rebuilt_first_wave_above_c1_does_not_pass(monkeypatch, tmp_path):
    """c=2 rebuilt from its first wave would meet the SLOs on idle-server
    latencies; the search must treat it as not passing and stop at c=1."""
    def levels(level, confirm):
        if level == 2:
            return {"output_tps_mean": 100.0, "total_tps_mean": 300.0,
                    "ttft_p99_ms": 100.0, "measured_requests": 2.0,
                    "warmup_included": 2.0}
        return None
    fake = _fake_probe(levels)
    result = _run_auto(monkeypatch, tmp_path, _auto_params(), fake)
    assert fake.calls == [(1, "probe"), (2, "probe"), (1, "confirm")]
    m = result.metrics
    assert m["reported_concurrency"] == 1
    assert m["c2"]["inconclusive"] == 1.0
    assert m["slo_fail_concurrency"] == 2.0


def test_auto_accepts_a_rebuilt_c1_level(monkeypatch, tmp_path):
    """At c=1 there is no queue to build, so a rebuilt level is usable — and
    flagged on the result."""
    rebuilt = {"output_tps_mean": 50.0, "total_tps_mean": 150.0,
               "ttft_p99_ms": 100.0, "measured_requests": 2.0, "warmup_included": 2.0}
    fake = _fake_probe(lambda level, confirm: rebuilt if level == 1 else
                       {"ttft_p99_ms": 1e9, "output_tps_mean": 1.0,
                        "total_tps_mean": 1.0, "measured_requests": 10.0})
    result = _run_auto(monkeypatch, tmp_path, _auto_params(), fake)
    m = result.metrics
    assert result.error is None
    assert m["reported_concurrency"] == 1
    assert m["c1"]["inconclusive"] == 0.0
    assert m["reported_level_meets_slo"] == 1.0
    assert m["reported_level_warmup_included"] == 2.0


def test_auto_less_than_one_wave_is_inconclusive(monkeypatch, tmp_path):
    fake = _fake_probe(lambda level, confirm: (
        {"output_tps_mean": 200.0, "total_tps_mean": 600.0, "ttft_p99_ms": 100.0,
         "measured_requests": 3.0} if level == 4 else None))
    result = _run_auto(monkeypatch, tmp_path, _auto_params(), fake)
    m = result.metrics
    assert m["c4"]["inconclusive"] == 1.0
    assert m["reported_concurrency"] == 3


def test_auto_empty_confirmation_keeps_the_probe_data(monkeypatch, tmp_path):
    """An empty run is no better than a crashed one: it must not overwrite a
    level the probe measured."""
    fake = _fake_probe(lambda level, confirm: _EMPTY if confirm else (
        None if level == 1 else {"ttft_p99_ms": 1e9, "output_tps_mean": 1.0,
                                 "total_tps_mean": 1.0, "measured_requests": 10.0}))
    result = _run_auto(monkeypatch, tmp_path, _auto_params(), fake)
    assert result.error is None
    assert result.metrics["reported_concurrency"] == 1
    assert "empty_window" not in result.metrics["c1"]
