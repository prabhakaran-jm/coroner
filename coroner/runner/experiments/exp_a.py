"""Experiment A: controlled fatal process exit via os._exit(1) (Task 6.14).

Requirements 9.1 to 9.12; design "Experiment A" flow. The Demo_App's /crash route ends the process with
os._exit(1). This module only POSTs /crash through the injected Demo_App request seam; it never kills anything.
Written against the frozen contract of `coroner.runner.experiments.base`.
"""
from __future__ import annotations

from coroner.runner.evaluate import evaluate_container_state
from coroner.runner.experiments.base import (
    TERMINATION_DESCRIPTIONS,
    DemoRequestError,
    ExperimentContext,
    ExperimentError,
    ExperimentResult,
    InspectFailed,
    PreambleResult,
    check_sample_captured_before,
    demo_request,
    drive_leak,
    durability_check,
    fetch_evidence,
    finalize_run,
    growth_check,
    IncompleteInspectError,
    poll_inspect,
    post_termination,
    read_inspect,
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

__all__ = ["CRASH_REQUEST_TIMEOUT_S", "EXIT_POLL_INTERVAL_S", "EXIT_TIMEOUT_S", "run"]

CRASH_REQUEST_TIMEOUT_S = 5.0
EXIT_TIMEOUT_S = 30.0
EXIT_POLL_INTERVAL_S = 0.25
SURVIVAL_WITHIN_S = 5.0


def run(ctx: ExperimentContext) -> ExperimentResult:
    """Run Experiment A and return the result (the manifest is already written)."""
    return run_experiment(ctx, _body)


BODY_EXCERPT_CHARS = 200


def _body_excerpt(body: object) -> str:
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body[: BODY_EXCERPT_CHARS * 4]).decode("utf-8", errors="replace")
    else:
        text = str(body)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:BODY_EXCERPT_CHARS]


def _crash_request_failed(ctx: ExperimentContext, pre: PreambleResult, response: object) -> ExperimentError:
    """Build the crash_request_failed error; take one immediate inspect reading (best effort, never raises)."""
    status = getattr(response, "status", None)
    excerpt = _body_excerpt(getattr(response, "body", b""))
    observed = "no inspect reading available"
    try:
        reading = read_inspect(ctx.docker.inspect(pre.container_id))
        observed = (
            f"immediate inspect: status={reading.status!r} exit_code={reading.exit_code!r} "
            f"oom_killed={reading.oom_killed!r}"
        )
        record_inspect(ctx, reading)
    except Exception as exc:  # best effort only: the failure must be reported whatever happens here
        if observed == "no inspect reading available":
            observed = f"no inspect reading available ({type(exc).__name__})"
    message = (
        f"POST /crash returned a completed HTTP response (status {status}, body excerpt {excerpt!r}) "
        f"instead of the expected dropped connection; a controlled fatal exit was not demonstrated; {observed}"
    )
    manifest = ctx.manifest
    if manifest is not None:
        limitations = manifest.extra.get("limitations")
        if not isinstance(limitations, list):
            limitations = []
            manifest.extra["limitations"] = limitations
        limitations.append(
            f"crash_request_failed: POST /crash returned HTTP {status} (body excerpt {excerpt!r}) instead of a "
            f"dropped connection; the termination description is the planned one, not an observed controlled "
            f"fatal exit; {observed}"
        )
    return ExperimentError("crash_request_failed", message)


def _body(ctx: ExperimentContext, pre: PreambleResult) -> ExperimentResult:
    manifest = ctx.manifest
    assert manifest is not None
    manifest.termination_description = TERMINATION_DESCRIPTIONS["A"]

    # 2 and 3: leak for at least 5 s; a failed or over-5-s request raises leak_failed before any /crash call.
    leak = drive_leak(ctx, min_duration_s=5.0, request_timeout_s=5.0, abort_on_failure=True)

    # 4: wait for post-leak RSS evidence.
    wait_after_leak(ctx, leak)

    # 5: record the time immediately before the request.
    crash_request_time = ctx.now()
    record_time(ctx, "crash_request_time", crash_request_time)
    write_phase(ctx, "crash_requested")

    # 6: the dropped connection is the expected outcome. A completed HTTP response of any status is a failure.
    try:
        response = demo_request(ctx, "POST", "/crash", timeout_s=CRASH_REQUEST_TIMEOUT_S)
    except DemoRequestError:
        response = None
    if response is not None:
        raise _crash_request_failed(ctx, pre, response)

    # 7: poll Docker inspect up to 30 s for the exit.
    poll = poll_inspect(ctx, pre.container_id, until_exited, EXIT_POLL_INTERVAL_S, EXIT_TIMEOUT_S)
    poll_reading = poll.reading
    if poll_reading is None and poll.result is not None:
        poll_reading = read_inspect(poll.result)
    if poll_reading is not None:
        record_inspect(ctx, poll_reading)  # keeps the raw inspect output even when the reading is incomplete
    if poll_reading is None or poll.outcome == "inspect_failed":
        raise InspectFailed(f"docker inspect gave no usable result: {poll.error or 'incomplete data'}")
    if not poll.matched:
        raise ExperimentError(
            "timeout",
            f"container did not exit within {EXIT_TIMEOUT_S:g} s of the crash request "
            f"(last observed status {poll_reading.status!r})",
        )

    # 8 and 9: the real ExitCode must be 1 and OOMKilled false, from real inspect values only.
    try:
        state = to_eval_state(poll_reading)
    except IncompleteInspectError as exc:
        raise InspectFailed(str(exc)) from exc
    evaluation = evaluate_container_state("A", state)

    # 12 (part): termination record with the cause label derived from the observed state.
    termination = post_termination(ctx, poll_reading)

    # 10: Collector survival within 5 s.
    survival = verify_collector_survival(ctx, SURVIVAL_WITHIN_S)

    # 12 (part): evidence copy, durability comparison.
    bundle = fetch_evidence(ctx)
    record_memory(ctx, bundle.samples, reading=poll_reading, include_block=False)
    durability = durability_check(ctx, bundle)

    # 11: a Sample captured before crash_request_time after clock correction (ts - clock_offset_ms).
    precrash = check_sample_captured_before(
        "at_least_one_sample_before_crash_request", bundle.samples, crash_request_time, pre.clock_offset_ms
    )

    # 12 (part): RSS growth against the leak.
    growth = growth_check(bundle.samples, leak, pre.clock_offset_ms)

    return finalize_run(
        ctx,
        evaluation=evaluation,
        extra_checks=(survival.check, precrash, termination.check, durability, growth),
    )
