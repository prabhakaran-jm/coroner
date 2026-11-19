"""Experiment C: genuine Docker OOM kill (Task 6.16).

Requirements 11.1 to 11.14, 11.17 to 11.20; design "Experiment C: genuine Docker OOM kill".

The runner leaks memory through the Demo_App until the kernel OOM-kills the container, then reads what Docker
says. It never calls `DockerPort.kill`, never posts to `/crash`, never sends a signal, and never invents an
exit code or an `OOMKilled` flag. `docker inspect` is the only source of truth. Diagnostic_Metrics and
Sampling_Limitations are recorded after the verdict and never feed it.

Written against the frozen `base.py` contract. Entry point: `run(ctx)`.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

from coroner.runner.docker_port import DockerError
from coroner.runner.evaluate import (
    Check,
    SampleView,
    diagnostic_metrics,
    evaluate_experiment_c,
    sampling_limitations,
)
from coroner.runner.experiments.base import (
    ExperimentContext,
    ExperimentError,
    ExperimentResult,
    InspectFailed,
    InspectReading,
    PreambleResult,
    drive_leak,
    durability_check,
    fetch_evidence,
    finalize_run,
    memory_limit_check,
    poll_inspect,
    post_termination,
    read_evidence_bundle,
    read_inspect,
    record_inspect,
    record_memory,
    run_experiment,
    to_eval_state,
    to_inspect_record,
    until_not_running,
    until_running,
    verify_collector_survival,
    write_phase,
)

__all__ = [
    "DEFAULT_SAMPLE_INTERVAL_MS",
    "DRIVE_MAX_S",
    "INSPECT_POLL_S",
    "INSPECT_WITHIN_S",
    "LEAK_FAILURE_SETTLE_S",
    "LEAK_INTERVAL_S",
    "START_WAIT_S",
    "run",
]

START_WAIT_S = 30.0  # Demo_App must be running within this long (11.2)
START_POLL_S = 0.5
LEAK_INTERVAL_S = 1.0  # leak calls at intervals of at most 1 s (11.3)
DRIVE_MAX_S = 120.0  # abort after this long since the first leak call (11.7)
INSPECT_WITHIN_S = 5.0  # inspect the stopped container within this long (11.8)
INSPECT_POLL_S = 0.25
LEAK_FAILURE_SETTLE_S = 3.0  # after a failed leak request, wait this long for an exit in progress before leak_failed
DEFAULT_SAMPLE_INTERVAL_MS = 500  # the Compose default when the runner passes no override


def run(ctx: ExperimentContext) -> ExperimentResult:
    """Run Experiment C. The common preamble, manifest handling and error mapping live in `run_experiment`."""
    return run_experiment(ctx, _body)


def _body(ctx: ExperimentContext, pre: PreambleResult) -> ExperimentResult:
    try:
        return _drive_and_evaluate(ctx, pre)
    except ExperimentError:
        _ensure_diagnostics(ctx)
        raise


# --------------------------------------------------------------------------------------------- flow


def _drive_and_evaluate(ctx: ExperimentContext, pre: PreambleResult) -> ExperimentResult:
    manifest = ctx.manifest
    assert manifest is not None
    container_id = pre.container_id

    # 2. confirm the Demo_App container is running within 30 s (11.2); no leak call before this
    started = poll_inspect(ctx, container_id, until_running, START_POLL_S, START_WAIT_S)
    if started.outcome == "inspect_failed":
        if started.reading is not None:
            record_inspect(ctx, started.reading, save_raw=True)  # keep the raw output and container_state_problems
        raise InspectFailed(f"Container_State could not be read: {started.error}")
    if not started.matched:
        raise ExperimentError("demo_app_failed_to_start", "Demo_App failed to start: container not running within 30 s")

    # 3-8. leak 4 MiB (or ctx.leak_chunk_bytes) chunks, at most 1 s apart, until the container stops or 120 s pass
    def container_stopped() -> bool:
        try:
            reading = read_inspect(ctx.docker.inspect(container_id))
        except (DockerError, ValueError):
            return False  # an unreadable inspect is not evidence of a stop; leak calls keep the run honest
        return reading.status is not None and reading.status != "running"

    leak = drive_leak(
        ctx,
        chunk_bytes=ctx.leak_chunk_bytes,
        min_duration_s=None,
        max_duration_s=DRIVE_MAX_S,
        interval_s=LEAK_INTERVAL_S,
        stop_when=container_stopped,
        abort_on_failure=False,
    )

    # 9-10. wait for the stop (a failed leak call may be the kill itself) and inspect
    # A failed leak request is judged by Docker: a stopped container (e.g. the OOM kill itself) continues to the
    # normal termination evaluation; a container still running after a short settle window is a leak failure.
    request_failed = leak.reason == "request_failed"
    if leak.reason == "stopped":
        wait_s = INSPECT_WITHIN_S
    elif request_failed:
        wait_s = LEAK_FAILURE_SETTLE_S
    else:  # "timeout": only what is left of the 120 s is waited
        wait_s = max(0.0, DRIVE_MAX_S - leak.duration_s)
    polled = poll_inspect(ctx, container_id, until_not_running, INSPECT_POLL_S, wait_s)

    if polled.outcome == "inspect_failed" or polled.reading is None:
        if polled.reading is not None and polled.reading.result is not None:
            record_inspect(ctx, polled.reading)  # keep whatever raw output exists
        raise InspectFailed(f"Container_State could not be read: {polled.error}")
    reading = polled.reading

    # 9. still running: leave the container running, record that, never stop or kill it
    if not polled.matched:
        manifest.extra["container_left_running"] = True
        record_inspect(ctx, reading)  # raw inspect and the observed (running) state
        samples = _host_clock(_best_effort_samples(ctx), pre.clock_offset_ms)
        record_memory(ctx, samples, reading=reading, include_block=True)
        _write_diagnostics(ctx, samples, reading)
        if request_failed:
            raise ExperimentError(
                "leak_failed",
                f"{leak.error}; container still running, leak calls stopped, container left running",
            )
        detail = f"; last leak error: {leak.error}" if leak.error else ""
        raise ExperimentError(
            "timeout",
            f"timeout elapsed: container still running after {DRIVE_MAX_S:g} s; "
            f"leak calls stopped, container left running{detail}",
        )

    # 11-13. full raw docker_inspect.json, inspect_time and the 64-hex Id are recorded from the reading
    record_inspect(ctx, reading)
    raw_path = manifest.extra.get("docker_inspect_path")

    # Collector side: termination record, survival, evidence
    post = post_termination(ctx, reading)
    survival = verify_collector_survival(ctx)
    try:
        bundle = fetch_evidence(ctx)
    except ExperimentError:
        _write_diagnostics(ctx, (), reading)
        raise
    samples = _host_clock(bundle.samples, pre.clock_offset_ms)

    # 14-15. HostConfig.Memory and the highest non-null RSS
    record_memory(ctx, samples, reading=reading, include_block=True)

    # 16. verdict from Docker's own values only (the six mandatory checks)
    state = to_eval_state(reading) if reading.complete else None
    record = to_inspect_record(reading, raw_path)
    stop_time = reading.finished_at or reading.inspect_time
    limit = reading.memory or ctx.mem_limit_bytes or 0
    evaluation = evaluate_experiment_c(state, survival.health, samples, stop_time, record, limit)

    # 17. diagnostics and sampling_limitations are written after the verdict, whatever it was
    _write_diagnostics(ctx, samples, reading)

    extra_checks: list[Check] = [memory_limit_check(ctx, reading), durability_check(ctx, bundle), post.check]
    return finalize_run(ctx, evaluation=evaluation, extra_checks=extra_checks)


# --------------------------------------------------------------------------------------------- diagnostics


def _host_clock(samples: Any, clock_offset_ms: int) -> tuple[SampleView, ...]:
    """Samples with recv_time (Collector clock) converted to the host clock; ts and RSS are untouched."""
    offset = timedelta(milliseconds=clock_offset_ms)
    return tuple(replace(s, recv_time=None if s.recv_time is None else s.recv_time - offset) for s in samples)


def _best_effort_samples(ctx: ExperimentContext) -> tuple[SampleView, ...]:
    """Samples from the Collector when they can be read, else none. Never raises."""
    try:
        return read_evidence_bundle(ctx).samples
    except Exception:  # noqa: BLE001 - evidence is optional on an aborted path
        return ()


def _leak_rate_bytes_per_s(ctx: ExperimentContext) -> int | None:
    assert ctx.manifest is not None
    leak = ctx.manifest.extra.get("leak")
    if not isinstance(leak, dict):
        return None
    total, duration = leak.get("cumulative_bytes"), leak.get("duration_s")
    if type(total) is int and total > 0 and isinstance(duration, (int, float)) and duration > 0:
        return int(total / duration)
    return None


def _write_diagnostics(ctx: ExperimentContext, samples: Any, reading: InspectReading | None) -> None:
    """Set `diagnostics` and `sampling_limitations` in the manifest extra (persisted by the next phase write)."""
    assert ctx.manifest is not None
    limit = (reading.memory if reading is not None else None) or ctx.mem_limit_bytes
    metrics = diagnostic_metrics(samples, limit if limit and limit > 0 else 1)
    if not limit or limit <= 0:
        metrics = replace(metrics, peak_rss_ratio_of_limit=None)
    interval = ctx.sample_interval_ms if ctx.sample_interval_ms is not None else DEFAULT_SAMPLE_INTERVAL_MS
    limits = sampling_limitations(interval, _leak_rate_bytes_per_s(ctx))
    ctx.manifest.extra["diagnostics"] = metrics.to_dict()
    ctx.manifest.extra["sampling_limitations"] = limits.to_dict()


def _ensure_diagnostics(ctx: ExperimentContext) -> None:
    """On any failure path that has not written them yet, record diagnostics from the evidence that exists."""
    manifest = ctx.manifest
    if manifest is None:
        return
    if manifest.extra.get("diagnostics") is not None and manifest.extra.get("sampling_limitations") is not None:
        return
    samples = _best_effort_samples(ctx) if manifest.extra.get("leak") is not None else ()
    _write_diagnostics(ctx, samples, None)
    try:
        write_phase(ctx, "diagnostics_recorded")
    except ExperimentError:
        pass  # fail_run writes the manifest again and reports a write failure itself
