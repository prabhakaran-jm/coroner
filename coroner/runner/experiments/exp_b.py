"""Experiment B: SIGKILL sent by the runner (Task 6.15).

Requirements 10.1 to 10.13; design "Experiment B: SIGKILL" and Property 27. Expected: exit code 137, OOMKilled false.

Plug-in shape (see `base.py`, FROZEN INTERFACE): `run(ctx)` runs the common preamble and `_body`. The body never
prints and never exits. Every failure is an `ExperimentError` that `run_experiment` turns into a manifest.

Flow
----
1. common preamble (`run_experiment`)
2. leak for at least 5 s (`drive_leak`; a failed or timed-out request raises `leak_failed`, so step 4 never runs)
3. wait for one post-leak Sample (`wait_after_leak`, needed by the growth check)
4. `sigkill_sent_time` recorded immediately before the kill call
5. `DockerPort.kill(<full 64-hex id>, "KILL")`
6. poll `docker inspect` every 200 ms for up to 10 s
7. require Status "exited" (otherwise `timeout`)
8. exit code 137 and OOMKilled false (checked by `evaluate_experiment_b`)
9. `State.FinishedAt` read from the inspect data (`InspectReading.finished_at`). It is OPTIONAL and never a verdict
   condition: when absent, unreadable or the zero value, `finished_at` is None, `classify_b_samples` skips the
   after_container_stop analysis, and the manifest says so (see `_record_timing`)
10. termination post (the cause label follows the observed state)
11. Collector survival
12. evidence fetch
13. `classify_b_samples` with the recorded clock offset (capture-time classification)
14. `evaluate_experiment_b` with the COMPUTED `stopped_within_10s` and `no_shutdown_handler_records`
15. `sample_timing` written to the manifest as soon as it is known (success and failure)
"""
from __future__ import annotations

from datetime import datetime, timedelta

from coroner.runner.docker_port import DockerError
from coroner.runner.evaluate import (
    CLOCK_TOLERANCE_MS,
    BTiming,
    SampleView,
    classify_b_samples,
    evaluate_experiment_b,
)
from coroner.runner.experiments.base import (
    TERMINATION_DESCRIPTIONS,
    EvidenceBundle,
    ExperimentContext,
    ExperimentError,
    ExperimentResult,
    InspectFailed,
    PreambleResult,
    check_no_shutdown_records,
    drive_leak,
    durability_check,
    fetch_evidence,
    finalize_run,
    growth_check,
    poll_inspect,
    post_termination,
    record_inspect,
    record_memory,
    record_time,
    run_experiment,
    to_eval_state,
    until_exited,
    verify_collector_survival,
    wait_after_leak,
    write_phase,
)

__all__ = ["KILL_SIGNAL", "POLL_INTERVAL_S", "STOP_TIMEOUT_S", "run", "timing_views"]

KILL_SIGNAL = "KILL"
POLL_INTERVAL_S = 0.2
STOP_TIMEOUT_S = 10.0
AFTER_STOP_UNAVAILABLE = "unavailable"
FINISHED_AT_LIMITATION = (
    "State.FinishedAt was missing or unreadable: container_finished_at is null and the after_container_stop "
    "analysis is unavailable. This does not affect the verdict."
)


def run(ctx: ExperimentContext) -> ExperimentResult:
    return run_experiment(ctx, _body)


def timing_views(samples: tuple[SampleView, ...] | list[SampleView], clock_offset_ms: int) -> list[SampleView]:
    """Samples for `classify_b_samples`. `ts` stays on the container clock (the classifier applies the offset to it).
    `recv_time` is on the Collector clock and is compared with the host-clock kill time, so it is converted here."""
    offset = timedelta(milliseconds=clock_offset_ms)
    return [
        SampleView(
            ts=s.ts,
            recv_time=None if s.recv_time is None else s.recv_time - offset,
            rss_bytes=s.rss_bytes,
            seq=s.seq,
        )
        for s in samples
    ]


def _classify(
    bundle: EvidenceBundle, sigkill_sent: datetime, finished_at: datetime | None, clock_offset_ms: int
) -> tuple[list[SampleView], BTiming]:
    views = timing_views(bundle.samples, clock_offset_ms)
    timing = classify_b_samples(views, sigkill_sent, finished_at, CLOCK_TOLERANCE_MS, clock_offset_ms)
    return views, timing


def _record_timing(
    ctx: ExperimentContext, bundle: EvidenceBundle, sigkill_sent: datetime, finished_at: datetime | None, offset: int
) -> list[SampleView]:
    views, timing = _classify(bundle, sigkill_sent, finished_at, offset)
    assert ctx.manifest is not None
    block = timing.to_dict()
    if finished_at is None:
        # State.FinishedAt is optional: nothing is fabricated and the stop-time analysis is reported as unavailable.
        block["after_container_stop_analysis"] = AFTER_STOP_UNAVAILABLE
        limitations = ctx.manifest.extra.get("limitations")
        if isinstance(limitations, list) and FINISHED_AT_LIMITATION not in limitations:
            limitations.append(FINISHED_AT_LIMITATION)
    else:
        block["after_container_stop_analysis"] = "available"
    ctx.manifest.extra["sample_timing"] = block
    write_phase(ctx, "samples_classified")
    return views


def _timing_best_effort(
    ctx: ExperimentContext, sigkill_sent: datetime, finished_at: datetime | None, offset: int
) -> None:
    """After the kill, when the run is about to fail before the evidence step: keep the timing if it is obtainable."""
    try:
        bundle = fetch_evidence(ctx)
        _record_timing(ctx, bundle, sigkill_sent, finished_at, offset)
    except (ExperimentError, ValueError, TypeError):
        pass


def _body(ctx: ExperimentContext, pre: PreambleResult) -> ExperimentResult:
    manifest = ctx.manifest
    assert manifest is not None
    manifest.termination_description = TERMINATION_DESCRIPTIONS["B"]
    offset = pre.clock_offset_ms

    # 2 and 3: leak for at least 5 s. A failed or over-5-s request raises leak_failed: no kill is ever sent.
    leak = drive_leak(ctx)
    wait_after_leak(ctx, leak)

    # 4 and 5: the time is taken immediately before the kill of the full validated container id.
    sigkill_sent = ctx.now()
    record_time(ctx, "sigkill_sent_time", sigkill_sent)
    try:
        ctx.docker.kill(pre.container_id, KILL_SIGNAL)
    except (DockerError, ValueError) as exc:
        raise ExperimentError("docker_kill_failed", f"docker kill failed: {type(exc).__name__}: {exc}") from exc
    write_phase(ctx, "sigkill_sent")

    # 6 and 7: poll every 200 ms for up to 10 s until the container has exited.
    poll = poll_inspect(ctx, pre.container_id, until_exited, POLL_INTERVAL_S, STOP_TIMEOUT_S)
    reading = poll.reading
    if poll.outcome == "inspect_failed":
        if reading is not None:
            record_inspect(ctx, reading)
        raise InspectFailed(f"docker inspect gave no complete reading: {poll.error}")
    assert reading is not None
    finished_at = reading.finished_at  # 9: the actual State.FinishedAt from the inspect data
    stopped_within_10s = poll.matched and poll.elapsed_s <= STOP_TIMEOUT_S
    record_inspect(ctx, reading)
    if not poll.matched:
        _timing_best_effort(ctx, sigkill_sent, finished_at, offset)
        raise ExperimentError(
            "timeout",
            f"the container did not stop within {STOP_TIMEOUT_S:g} s of the SIGKILL (last status {reading.status!r})",
        )

    # 10 to 12: termination post, Collector survival, evidence.
    post = post_termination(ctx, reading)
    survival = verify_collector_survival(ctx)
    bundle = fetch_evidence(ctx)

    # 13: classification by offset-corrected Capture_Time.
    views = _record_timing(ctx, bundle, sigkill_sent, finished_at, offset)
    record_memory(ctx, bundle.samples, reading=reading, include_block=False)

    # 14: both computed values come from observation. check_no_shutdown_records scans the whole evidence.
    shutdown_check = check_no_shutdown_records(bundle)
    evaluation = evaluate_experiment_b(
        to_eval_state(reading),
        survival.health,
        views,
        sigkill_sent,
        finished_at,
        offset,
        CLOCK_TOLERANCE_MS,
        stopped_within_10s=stopped_within_10s,
        no_shutdown_handler_records=shutdown_check.passed,
    )
    extra = [
        post.check,
        durability_check(ctx, bundle),
        growth_check(bundle.samples, leak, offset),
    ]
    return finalize_run(ctx, evaluation=evaluation, extra_checks=extra)
