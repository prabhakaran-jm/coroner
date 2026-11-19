"""Common foundation for Experiments A, B and C (Task 6.13).

Requirements 4.16, 7.4, 8.7, 8.14, 9.1, 9.2, 12.1, 12.7; design "Experiment flows" and "Error Handling".

FROZEN INTERFACE (Batch 3)
==========================

Experiments A, B and C (`exp_a.py`, `exp_b.py`, `exp_c.py`) are written against this contract and must not edit
this module. Everything here is standard library plus `coroner.common`, `coroner.evidence` and the other
`coroner.runner` modules. No real Docker, socket, sleep or clock is touched unless the caller leaves the default
seams in `ExperimentContext`.

Plug-in shape
-------------
Each experiment module defines::

    def run(ctx: ExperimentContext) -> ExperimentResult:
        return run_experiment(ctx, _body)

    def _body(ctx: ExperimentContext, pre: PreambleResult) -> ExperimentResult:
        ...   # drive the leak, crash/kill/OOM, poll inspect, record, then `finalize_run(...)`

`run_experiment` runs the preamble, calls the body, and turns every `ExperimentError` (and any unexpected error)
into a failed or aborted manifest plus an `ExperimentResult`. The body returns `finalize_run(...)` or raises
`ExperimentError(code, message)`. The body never prints and never calls `sys.exit`; the CLI prints
`ExperimentResult.line` and exits with `ExperimentResult.exit_code`.

Exit codes (`ExperimentResult.exit_code`)
-----------------------------------------
EXIT_PASSED = 0       the experiment passed.
EXIT_FAILED = 1       the experiment failed or aborted after the run id was created. A readable manifest exists
                      (`ExperimentResult.manifest_path`) unless the manifest itself could not be written
                      (`manifest_path is None`, the message says why).
EXIT_ENVIRONMENT = 2  usage or environment error. Before a run id exists (`PreRunError`: Docker unavailable,
                      invalid `--mem-limit` or leak chunk, unusable runs directory): `run_id is None`, NO manifest.
                      After a run id exists (only `docker_compose_failed`, where `docker compose` itself exits
                      non-zero): a manifest IS written and the exit code is 2.

Failure codes (`FAILURE_TABLE`: code -> (manifest outcome, exit code))
----------------------------------------------------------------------
Before a run id exists (`PreRunError`, outcome "not_started", exit 2, no manifest):
  docker_unavailable, invalid_arguments, environment_error
Aborted (exit 1 unless noted): collector_unhealthy, collector_storage_unhealthy, clock_offset_unavailable,
  run_id_collision, docker_compose_failed (exit 2), invalid_container_id, demo_app_failed_to_start,
  no_baseline_sample, leak_failed, interrupted, internal_error
Failed (exit 1): timeout, unexpected_exit_code, oomkilled_mismatch, docker_inspect_failed, docker_kill_failed, crash_request_failed,
  evidence_unavailable, collector_survival_failed, no_memory_growth_observed, no_precrash_evidence, sample_captured_after_kill,
  durability_mismatch, shutdown_handler_records_found, termination_post_failed, check_failed,
  manifest_write_failed
`run_incomplete` is the placeholder failure of a manifest that was created but never finished (outcome "aborted").
A manifest whose `extra["finished"]` is False was interrupted or is still running.

Types
-----
class ExperimentError(Exception):  # code, message, outcome ("failed"|"aborted"), exit_code, .failure -> Failure
    def __init__(self, code: str, message: str, *, outcome: str | None = None, exit_code: int | None = None)
class PreRunError(ExperimentError)          # no run id, no manifest, exit 2
class ManifestWriteError(ExperimentError)   # code "manifest_write_failed"
class EvidenceUnavailable(ExperimentError)  # code "evidence_unavailable"; attribute .http_status: int | None
class InspectFailed(ExperimentError)        # code "docker_inspect_failed"
class DemoRequestError(Exception)           # a Demo_App HTTP request got no response (also: the dropped /crash connection)
class IncompleteInspectError(ValueError)    # attribute .problems: tuple[str, ...]

@dataclass class ExperimentContext(
    experiment: str,                        # "A" | "B" | "C" (upper-cased; anything else raises ValueError)
    docker: DockerPort,
    collector: CollectorClient,
    runs_dir: Path | str,
    mem_limit: str | None = "128m",         # CORONER_DEMO_MEM_LIMIT override; None = not passed (compose default)
    leak_chunk_bytes: int = 4194304,        # 1..104857600
    sample_interval_ms: int | None = None,  # CORONER_SAMPLE_INTERVAL_MS override; None = not passed (compose default 500)
    demo_url: str = "http://127.0.0.1:8080",
    http: Transport | None = None,          # (method, url, body, timeout_s) -> HttpResponse; default urllib_transport()
    wall_clock: Callable[[], datetime] = utc now (aware),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    rand_hex: Callable[[], str] | None = None,   # 8 lowercase hex for the run id; None = secrets
  )
  Preamble state, read-only for experiments: run_id: str | None, run_dir: Path | None, manifest: RunManifest | None,
  mem_limit_bytes: int | None, container_id: str | None, durability: str | None, clock_offset_ms: int | None,
  clock_offset_uncertainty_ms: float | None.
  Methods: now() -> datetime (aware UTC, truncated to ms, from wall_clock); stamp() -> str (format_utc_ms(now())).
  Sign convention of clock_offset_ms: server_time - host midpoint. Host-equivalent time = container time - offset,
  which is what `classify_b_samples(..., clock_offset_ms=ctx.clock_offset_ms)` and `host_time()` apply.

@dataclass(frozen=True) class PreambleResult:
    run_id: str; container_id: str (64 lowercase hex); clock_offset_ms: int; clock_offset_uncertainty_ms: float
    durability: str | None; demo_health: dict | None; baseline: EvidenceBundle; first_sample: SampleView

@dataclass(frozen=True) class ExperimentResult:
    exit_code: int; outcome: str ("passed" | "failed" | "aborted" | "not_started"); experiment: str
    run_id: str | None; manifest_path: Path | None; message: str; failure: Failure | None; checks: tuple[Check, ...]
    phase: str | None; property line -> str ("Experiment A <run id>: PASSED - <message>")

Entry points
------------
run_experiment(ctx: ExperimentContext, body: Callable[[ExperimentContext, PreambleResult], ExperimentResult]) -> ExperimentResult
run_preamble(ctx: ExperimentContext) -> PreambleResult        # raises PreRunError before a run id exists, else ExperimentError
finalize_run(ctx, *, evaluation: Evaluation | None = None, extra_checks: Sequence[Check] = ()) -> ExperimentResult
fail_run(ctx, error: ExperimentError) -> ExperimentResult     # writes the failure manifest; used by run_experiment
pre_run_result(ctx, error: PreRunError) -> ExperimentResult   # no manifest

Manifest writer and updater
---------------------------
`ctx.manifest` is the live `RunManifest`. Experiments may mutate `ctx.manifest.extra[...]`, `.container_state`,
`.evidence`, `.termination_description`, then persist with:
write_phase(ctx, phase: str, *, updates: Mapping[str, Any] | None = None) -> Path
    Atomic (temp file, fsync, os.replace) rewrite of runs/<run_id>/manifest.json. Sets extra["phase"], appends
    {"name", "time"} to extra["phases"], sets end_time to the write time, applies `updates` to `extra` first.
    Raises ManifestWriteError. Every helper below that is a phase (preamble steps, drive_leak, record_inspect,
    post_termination, verify_collector_survival, fetch_evidence) calls it itself.
record_time(ctx, key: str, instant: datetime) -> None         # extra["times"][key] = format_utc_ms(instant); key is
                                                              # "crash_request_time" or "sigkill_sent_time"; no write
host_time(ctx, container_time: datetime) -> datetime          # container_time - clock_offset_ms
MANIFEST_EXTRA_KEYS: tuple[str, ...]                          # every experiment writes exactly this key set (null when not applicable)
A manifest is created before the experiment starts with outcome "aborted" and failure code `run_incomplete`,
`extra["finished"] False`. `finalize_run` or `fail_run` replaces that with the real outcome and sets finished True.
extra["checks"] is a list of {"name", "passed"[, "detail"]}. extra["times"] is
{"crash_request_time": None, "sigkill_sent_time": None} until set. `container_left_running` defaults False.
`sample_timing` is None except in B (set `ctx.manifest.extra["sample_timing"] = timing.to_dict()`); `memory`,
`diagnostics` and `sampling_limitations` are None except in C (see `record_memory`).

Docker state conversion (InspectResult -> typed readings)
---------------------------------------------------------
read_inspect(result: InspectResult) -> InspectReading         # never raises; missing or ill-typed data is listed in
    .problems and the field is None. Nothing is defaulted or fabricated. .complete is True only when Id (64 lowercase
    hex), State.Status, State.OOMKilled (bool), State.ExitCode (int, not bool), HostConfig.Memory (int >= 0) and an
    aware inspect_time are all present. .finished_at is State.FinishedAt (None when absent, unparseable or the zero value).
    InspectReading fields: inspect_time, id, status, oom_killed, exit_code, memory, finished_at, finished_at_raw,
    problems, result (the InspectResult, for the raw bytes). Properties: complete, excerpt (raises IncompleteInspectError).
to_eval_state(reading: InspectReading) -> evaluate.ContainerState          # raises IncompleteInspectError when incomplete
to_manifest_state(reading: InspectReading, raw_inspect_path: str | PurePath) -> manifest.ContainerState   # same
to_inspect_record(reading: InspectReading, raw_inspect_path: str | PurePath | None) -> evaluate.InspectRecord   # never raises
to_termination_state(reading: InspectReading) -> dict                       # {"status", "exit_code", "oom_killed"}; raises when incomplete
derive_cause_label(experiment: str, state: InspectReading | evaluate.ContainerState | None) -> str
    "controlled_fatal_process_exit" (A), "sigkill_sent_by_runner" (B), "docker_oom_kill" (C) only when the observed
    state equals the experiment's expected state with status "exited" (evaluate.evaluate_container_state); else "unknown".
record_inspect(ctx, reading: InspectReading, *, save_raw: bool = True) -> manifest.ContainerState | None
    Saves the raw bytes unchanged to runs/<run_id>/docker_inspect.json (even when the reading is incomplete), sets
    ctx.manifest.container_state (None when incomplete), extra["docker_inspect_path"], extra["container_finished_at"],
    extra["container_state_problems"], then write_phase("container_inspected"). Raises InspectFailed if the raw file
    cannot be saved.

Inspect polling
---------------
poll_inspect(ctx, container_id: str, until: Callable[[InspectReading], bool], interval_s: float, timeout_s: float) -> PollResult
    Calls ctx.docker.inspect, `read_inspect`, and `until(reading)` (only for complete readings), sleeping
    min(interval_s, remaining) via ctx.sleep and measuring with ctx.monotonic. Always attempts at least once and once
    more at the deadline. DockerError/ValueError from inspect are remembered (PollResult.error) and polling continues.
    PollResult: outcome "matched" | "timeout" | "inspect_failed" ("inspect_failed" means the final attempt at the deadline
    was an incomplete reading or an inspect error; only the last observation decides, a complete non-matching
    last reading is "timeout"), reading (last reading, the matching one when matched), result (its InspectResult), attempts,
    elapsed_s, error; property matched.
inspect_once(ctx, container_id: str) -> InspectReading     # raises InspectFailed on a Docker error
until_exited(r) / until_running(r) / until_not_running(r)  # predicates on InspectReading.status

Leak driver (shared)
--------------------
drive_leak(ctx, *, chunk_bytes: int | None = None, min_duration_s: float | None = 5.0, max_duration_s: float | None = None,
           interval_s: float = 1.0, request_timeout_s: float = 5.0, stop_when: Callable[[], bool] | None = None,
           abort_on_failure: bool = True) -> LeakResult
    POST {demo_url}/leak?bytes=<chunk> every interval_s (slot-based, first call at once, 5 s request timeout; a
    reply later than the timeout counts as a failure). Stops with reason "complete" once min_duration_s has passed
    since the first call (A and B), "stopped" when stop_when() is true (checked before each request), "timeout" when
    max_duration_s has passed since the first call (C), or "request_failed". With abort_on_failure True (A and B) a
    failed request raises ExperimentError("leak_failed") AFTER recording the leak block, so the caller never reaches
    /crash or kill. C passes abort_on_failure=False and reads LeakResult.reason. At least one of min_duration_s,
    max_duration_s, stop_when is required (ValueError).
    Records extra["leak"] and write_phase("leak_finished" | "leak_failed").
    LeakResult: chunk_bytes, requests (tuple[LeakRequestRecord]), cumulative_bytes (sum acknowledged by HTTP 200),
    reason, error, first_call_time, last_response_time (aware, host clock), last_response_mono, duration_s;
    properties ok (reason == "complete"), request_count; method block() -> dict.
    LeakRequestRecord: index, sent_time, http_status, elapsed_s, ok, error, retained_bytes_total.
post_leak_wait_s(interval_ms: int | None) -> float          # max(1.0, 1.5*interval, 1.0 + interval + 0.25) seconds
wait_after_leak(ctx, leak: LeakResult) -> float             # sleeps until post_leak_wait_s after the last response; returns seconds slept
demo_request(ctx, method: str, path: str, *, timeout_s: float) -> HttpResponse   # raises DemoRequestError when no response
                                                                                   # (A: the dropped POST /crash connection)

Evidence retrieval
------------------
read_evidence_bundle(ctx) -> EvidenceBundle     # GET evidence via the Collector, parse with the Evidence_Reader;
                                                # raises EvidenceUnavailable; no manifest change
fetch_evidence(ctx) -> EvidenceBundle           # read_evidence_bundle + host copy runs/<run_id>/evidence.jsonl +
                                                # manifest.evidence + extra["evidence_summary"] + write_phase("evidence_fetched")
EvidenceBundle: raw (bytes), result (ReadResult), summary (RunSummary), samples (tuple[SampleView], capture-time
    order), durability_levels (tuple[str]), fetched_at; properties record_count, sample_count.

Termination post
----------------
post_termination(ctx, reading: InspectReading, *, cause_label: str | None = None, note: str | None = None) -> TerminationPost
    POST /v1/runs/<id>/termination {source, observed_time (= inspect time), container_state, cause_label, note}.
    cause_label defaults to derive_cause_label. Never raises for HTTP trouble. Records extra["termination"] and
    write_phase("termination_posted"). An incomplete reading is not posted (ok False).
TerminationPost: ok, http_status, cause_label, payload, error; property check -> Check("termination_recorded", ...).
    Add `post.check` to finalize_run(extra_checks=...).

Collector survival
------------------
verify_collector_survival(ctx, within_s: float = 5.0) -> SurvivalResult
    GET /healthz until HTTP 200 with storage_ok exactly True arrives within within_s (200 with storage not True fails
    at once; no response or non-200 is retried every 0.5 s inside the window). Records extra["collector_health"] =
    {http_status, storage_ok, ok, checked_time, latency_ms, elapsed_s, within_s} and write_phase("collector_survival").
    SurvivalResult: ok, health (HealthResult for evaluate_experiment_b/c; a reply later than within_s is returned as
    http_status None, never as a pass), elapsed_s, detail; property check -> Check("collector_survival", ...).

Checks and helpers
------------------
growth_check(samples: Sequence[SampleView], leak: LeakResult, clock_offset_ms: int, *, fraction: float = 0.5,
             min_after_s: float = 1.0, name: str = "memory_growth_matches_leak") -> Check      # A and B (Requirement 7.4)
durability_check(ctx, bundle: EvidenceBundle) -> Check("durability_matches_run_metadata")
check_no_shutdown_records(bundle: EvidenceBundle) -> Check("no_shutdown_handler_records")
check_sample_captured_before(name: str, samples, instant: datetime, clock_offset_ms: int) -> Check
memory_limit_check(ctx, reading: InspectReading) -> Check("memory_limit_matches_host_config")  # parse_mem_size(mem_limit) == HostConfig.Memory
first_rss_bytes(samples) -> int | None; highest_rss_bytes(samples) -> int | None     # capture order, null skipped, never 0
record_memory(ctx, samples, *, reading: InspectReading | None = None, include_block: bool) -> None
    Sets extra["highest_rss_bytes"]; with include_block (C) also extra["memory"] = {configured_limit_bytes,
    memory_limit_bytes (HostConfig.Memory or None), highest_rss_bytes, first_rss_bytes}. No write.
failure_for_checks(checks: Sequence[Check], message: str | None = None) -> Failure   # first failed check decides the code
checks_to_dicts(checks) -> list[dict]
Check-name to failure-code mapping: `CHECK_FAILURE_CODES` (exact names) and `CHECK_FAILURE_PREFIXES`; an unmapped
failed check gives code "check_failed". Names starting "exit_code" -> unexpected_exit_code, "oom_killed" ->
oomkilled_mismatch, "at_least_one_sample" -> no_precrash_evidence.

Error table mapping (design "Error Handling", runner rows)
----------------------------------------------------------
Docker unavailable -> PreRunError docker_unavailable, exit 2, no manifest.   Collector not healthy in 60 s ->
collector_unhealthy.  200 with storage_ok false/absent -> collector_storage_unhealthy, "Collector storage is not
healthy", Demo_App never started.  Demo_App not healthy in 30 s -> demo_app_failed_to_start.  Leak failure/5 s
timeout in A/B -> leak_failed, no crash or kill.  Container not stopping -> raise ExperimentError("timeout", ...)
after record_inspect of the last reading.  Wrong exit code / OOMKilled -> failed checks (unexpected_exit_code /
oomkilled_mismatch).  Inspect failure -> docker_inspect_failed.  C 120 s -> raise ExperimentError("timeout", ...)
after setting extra["container_left_running"] = True.  Evidence fetch failure -> evidence_unavailable.  Survival
failure -> failed check collector_survival (code collector_survival_failed).  Any failure after the run id exists
keeps the manifest and the evidence already stored.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath
from typing import Any

from coroner.common.timeutil import format_utc_ms, parse_utc_ms, utc_now_ms
from coroner.evidence.reader import UNOBSERVED_NOTE, ReadResult, RunSummary, read_evidence, summarize
from coroner.evidence.schema import FILE_SUFFIX, RECORD_TYPES
from coroner.runner.collector_client import (
    READINESS_WINDOW_S,
    ClockOffset,
    CollectorClient,
    CollectorUnavailable,
    HealthResult,
    HttpResponse,
    Transport,
    check_ready,
    urllib_transport,
)
from coroner.runner.docker_port import DockerError, DockerOutputError, DockerPort, InspectResult
from coroner.runner.evaluate import (
    EXPECTED_STATE,
    Check,
    Evaluation,
    InspectRecord,
    SampleView,
    evaluate_container_state,
    parse_mem_size,
)
from coroner.runner.evaluate import ContainerState as EvalContainerState
from coroner.runner.manifest import (
    ContainerState as ManifestContainerState,
    EvidenceRef,
    Expected,
    Failure,
    InspectExcerpt,
    ManifestError,
    RunIdError,
    RunIdExhaustedError,
    RunManifest,
    atomic_write_bytes,
    generate_run_id,
    posix_path,
    save_raw_inspect,
    write_manifest,
)

__all__ = [
    "CAUSE_UNKNOWN",
    "CHECK_FAILURE_CODES",
    "CHECK_FAILURE_PREFIXES",
    "DEFAULT_DEMO_URL",
    "DEFAULT_LEAK_CHUNK_BYTES",
    "DEFAULT_MEM_LIMIT",
    "EXIT_ENVIRONMENT",
    "EXIT_FAILED",
    "EXIT_PASSED",
    "FAILURE_TABLE",
    "MANIFEST_EXTRA_KEYS",
    "TERMINATION_DESCRIPTIONS",
    "DemoRequestError",
    "EvidenceBundle",
    "EvidenceUnavailable",
    "ExperimentContext",
    "ExperimentError",
    "ExperimentResult",
    "IncompleteInspectError",
    "InspectFailed",
    "InspectReading",
    "LeakRequestRecord",
    "LeakResult",
    "ManifestWriteError",
    "PollResult",
    "PreRunError",
    "PreambleResult",
    "SurvivalResult",
    "TerminationPost",
    "checks_to_dicts",
    "check_no_shutdown_records",
    "check_sample_captured_before",
    "demo_request",
    "derive_cause_label",
    "drive_leak",
    "durability_check",
    "fail_run",
    "failure_for_checks",
    "fetch_evidence",
    "finalize_run",
    "first_rss_bytes",
    "growth_check",
    "highest_rss_bytes",
    "host_time",
    "inspect_once",
    "memory_limit_check",
    "poll_inspect",
    "post_leak_wait_s",
    "post_termination",
    "pre_run_result",
    "read_evidence_bundle",
    "read_inspect",
    "record_inspect",
    "record_memory",
    "record_time",
    "run_experiment",
    "run_preamble",
    "to_eval_state",
    "to_inspect_record",
    "to_manifest_state",
    "to_termination_state",
    "until_exited",
    "until_not_running",
    "until_running",
    "verify_collector_survival",
    "wait_after_leak",
    "write_phase",
]

# --------------------------------------------------------------------------------------------- constants

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ENVIRONMENT = 2

DEMO_SERVICE = "demo_app"
DEFAULT_DEMO_URL = "http://127.0.0.1:8080"
DEFAULT_MEM_LIMIT = "128m"
DEFAULT_LEAK_CHUNK_BYTES = 4 * 1024 * 1024
MAX_LEAK_BYTES = 104_857_600  # the Demo_App's upper bound for one /leak request

DEMO_HEALTH_TIMEOUT_S = 30.0
DEMO_HEALTH_POLL_S = 0.5
DEMO_HEALTH_REQUEST_TIMEOUT_S = 2.0
FIRST_SAMPLE_TIMEOUT_S = 30.0
FIRST_SAMPLE_POLL_S = 0.5
LEAK_REQUEST_TIMEOUT_S = 5.0
LEAK_INTERVAL_S = 1.0
LEAK_MIN_DURATION_S = 5.0
SURVIVAL_WITHIN_S = 5.0
SURVIVAL_POLL_S = 0.5
CLOCK_OFFSET_RETRIES = 3
RUN_DIR_ATTEMPTS = 5

RAW_INSPECT_NAME = "docker_inspect.json"
EVIDENCE_COPY_NAME = "evidence.jsonl"
EVIDENCE_CONTAINER_DIR = "/data/evidence"

TERMINATION_SOURCE = "experiment_runner"
TERMINATION_NOTE = (
    "Observed after the fact via docker inspect. The interval between the last Sample and the actual termination "
    "is unobserved."
)
TERMINATION_DESCRIPTIONS: dict[str, str] = {
    "A": "controlled fatal process exit via os._exit(1)",
    "B": "SIGKILL sent by the runner (docker kill --signal=KILL)",
    "C": "genuine Docker OOM kill (kernel cgroup memory limit)",
}
CAUSE_LABELS_BY_EXPERIMENT: dict[str, str] = {
    "A": "controlled_fatal_process_exit",
    "B": "sigkill_sent_by_runner",
    "C": "docker_oom_kill",
}
CAUSE_UNKNOWN = "unknown"

PLACEHOLDER_FAILURE_CODE = "run_incomplete"

# code -> (manifest outcome or "not_started", exit code)
FAILURE_TABLE: dict[str, tuple[str, int]] = {
    # before a run id exists
    "docker_unavailable": ("not_started", EXIT_ENVIRONMENT),
    "invalid_arguments": ("not_started", EXIT_ENVIRONMENT),
    "environment_error": ("not_started", EXIT_ENVIRONMENT),
    # aborted before the experiment proper
    "collector_unhealthy": ("aborted", EXIT_FAILED),
    "collector_storage_unhealthy": ("aborted", EXIT_FAILED),
    "clock_offset_unavailable": ("aborted", EXIT_FAILED),
    "run_id_collision": ("aborted", EXIT_FAILED),
    "docker_compose_failed": ("aborted", EXIT_ENVIRONMENT),
    "invalid_container_id": ("aborted", EXIT_FAILED),
    "demo_app_failed_to_start": ("aborted", EXIT_FAILED),
    "no_baseline_sample": ("aborted", EXIT_FAILED),
    "leak_failed": ("aborted", EXIT_FAILED),
    "interrupted": ("aborted", EXIT_FAILED),
    "internal_error": ("aborted", EXIT_FAILED),
    PLACEHOLDER_FAILURE_CODE: ("aborted", EXIT_FAILED),
    # failed experiment
    "timeout": ("failed", EXIT_FAILED),
    "unexpected_exit_code": ("failed", EXIT_FAILED),
    "oomkilled_mismatch": ("failed", EXIT_FAILED),
    "docker_inspect_failed": ("failed", EXIT_FAILED),
    "docker_kill_failed": ("failed", EXIT_FAILED),
    "crash_request_failed": ("failed", EXIT_FAILED),
    "evidence_unavailable": ("failed", EXIT_FAILED),
    "collector_survival_failed": ("failed", EXIT_FAILED),
    "no_memory_growth_observed": ("failed", EXIT_FAILED),
    "no_precrash_evidence": ("failed", EXIT_FAILED),
    "sample_captured_after_kill": ("failed", EXIT_FAILED),
    "durability_mismatch": ("failed", EXIT_FAILED),
    "shutdown_handler_records_found": ("failed", EXIT_FAILED),
    "termination_post_failed": ("failed", EXIT_FAILED),
    "check_failed": ("failed", EXIT_FAILED),
    "manifest_write_failed": ("failed", EXIT_FAILED),
}

CHECK_FAILURE_CODES: dict[str, str] = {
    "collector_survival": "collector_survival_failed",
    "stopped_within_10s": "timeout",
    "stopped_within_30s": "timeout",
    "exited_within_30s": "timeout",
    "measurable_memory_growth": "no_memory_growth_observed",
    "memory_growth_matches_leak": "no_memory_growth_observed",
    "preserved_precrash_evidence": "no_precrash_evidence",
    "no_sample_captured_after_kill_beyond_tolerance": "sample_captured_after_kill",
    "docker_inspect_recorded": "docker_inspect_failed",
    "durability_matches_run_metadata": "durability_mismatch",
    "no_shutdown_handler_records": "shutdown_handler_records_found",
    "termination_recorded": "termination_post_failed",
}
CHECK_FAILURE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("exit_code", "unexpected_exit_code"),
    ("oom_killed", "oomkilled_mismatch"),
    ("at_least_one_sample", "no_precrash_evidence"),
)

# Every manifest carries exactly these keys in `extra`, whatever the experiment (null when not applicable).
MANIFEST_EXTRA_KEYS: tuple[str, ...] = (
    "phase",
    "phases",
    "finished",
    "container_id",
    "container_finished_at",
    "container_left_running",
    "docker_inspect_path",
    "container_state_problems",
    "collector",
    "clock_offset_ms",
    "clock_offset_uncertainty_ms",
    "memory",
    "highest_rss_bytes",
    "leak",
    "times",
    "collector_health",
    "termination",
    "checks",
    "diagnostics",
    "sampling_limitations",
    "sample_timing",
    "evidence_summary",
    "limitations",
)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_DOCKER_TIME = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z")
_KNOWN_RECORD_TYPES = frozenset(RECORD_TYPES)


# --------------------------------------------------------------------------------------------- errors


class ExperimentError(Exception):
    """A run failed or aborted. `code` is a key of FAILURE_TABLE (an unknown code defaults to failed, exit 1)."""

    def __init__(self, code: str, message: str, *, outcome: str | None = None, exit_code: int | None = None) -> None:
        super().__init__(message)
        spec = FAILURE_TABLE.get(code, ("failed", EXIT_FAILED))
        self.code = code
        self.message = message
        self.outcome = outcome if outcome is not None else spec[0]
        self.exit_code = exit_code if exit_code is not None else spec[1]

    @property
    def failure(self) -> Failure:
        return Failure(self.code, self.message)


class PreRunError(ExperimentError):
    """Raised before a run id exists. Exit code 2, no run id, no manifest."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, outcome="not_started", exit_code=EXIT_ENVIRONMENT)


class ManifestWriteError(ExperimentError):
    def __init__(self, message: str) -> None:
        super().__init__("manifest_write_failed", message)


class EvidenceUnavailable(ExperimentError):
    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__("evidence_unavailable", message)
        self.http_status = http_status


class InspectFailed(ExperimentError):
    def __init__(self, message: str) -> None:
        super().__init__("docker_inspect_failed", message)


class DemoRequestError(Exception):
    """A Demo_App HTTP request produced no response (connection error, reset, timeout)."""


class IncompleteInspectError(ValueError):
    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("incomplete docker inspect data: " + "; ".join(self.problems))


# --------------------------------------------------------------------------------------------- docker state


def _is_int(value: object) -> bool:
    return type(value) is int


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def parse_docker_time(text: object) -> datetime | None:
    """Parse a Docker timestamp (RFC 3339, up to 9 fractional digits, `Z`). The zero value gives None."""
    if not isinstance(text, str):
        return None
    match = _DOCKER_TIME.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if year <= 1:
        return None  # "0001-01-01T00:00:00Z": the container never started or finished
    micro = int(((match.group(7) or "") + "000000")[:6])
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class InspectReading:
    """One `docker inspect` observation. A field the data did not supply (or supplied with a wrong type) is None
    and named in `problems`; nothing is defaulted."""

    inspect_time: datetime | None
    id: str | None = None
    status: str | None = None
    oom_killed: bool | None = None
    exit_code: int | None = None
    memory: int | None = None
    finished_at: datetime | None = None
    finished_at_raw: str | None = None
    problems: tuple[str, ...] = ()
    result: InspectResult | None = field(default=None, compare=False, repr=False)

    @property
    def complete(self) -> bool:
        return not self.problems

    @property
    def excerpt(self) -> InspectExcerpt:
        if self.problems:
            raise IncompleteInspectError(self.problems)
        assert self.id is not None and self.status is not None and self.oom_killed is not None
        assert self.exit_code is not None and self.memory is not None
        return InspectExcerpt(self.id, self.status, self.oom_killed, self.exit_code, self.memory)


def read_inspect(result: InspectResult) -> InspectReading:
    """Build an `InspectReading` from `docker inspect` output. Never raises and never fabricates data."""
    problems: list[str] = []
    obj: Any = result.parsed
    if isinstance(obj, list) and len(obj) == 1:
        obj = obj[0]
    if not isinstance(obj, Mapping):
        obj = {}
        problems.append("inspect output is not a single JSON object")
    state = obj.get("State")
    host = obj.get("HostConfig")
    state = state if isinstance(state, Mapping) else {}
    host = host if isinstance(host, Mapping) else {}

    cid = obj.get("Id")
    if not (isinstance(cid, str) and _HEX64.fullmatch(cid)):
        problems.append("Id: expected 64 lowercase hex characters")
        cid = None
    status = state.get("Status")
    if not (isinstance(status, str) and status != ""):
        problems.append("State.Status: missing or not a non-empty string")
        status = None
    oom = state.get("OOMKilled")
    if type(oom) is not bool:
        problems.append("State.OOMKilled: missing or not a boolean")
        oom = None
    code = state.get("ExitCode")
    if not _is_int(code):
        problems.append("State.ExitCode: missing or not an integer")
        code = None
    memory = host.get("Memory")
    if not (_is_int(memory) and memory >= 0):
        problems.append("HostConfig.Memory: missing or not a non-negative integer")
        memory = None
    when = result.inspect_time
    if not _aware(when):
        problems.append("inspect_time: missing or not timezone-aware")
        when = None
    raw_finished = state.get("FinishedAt")
    return InspectReading(
        inspect_time=when,
        id=cid,
        status=status,
        oom_killed=oom,
        exit_code=code,
        memory=memory,
        finished_at=parse_docker_time(raw_finished),
        finished_at_raw=raw_finished if isinstance(raw_finished, str) else None,
        problems=tuple(problems),
        result=result,
    )


def to_eval_state(reading: InspectReading) -> EvalContainerState:
    """`evaluate.ContainerState` (exit code, OOMKilled, status). Raises IncompleteInspectError, never defaults."""
    if reading.problems:
        raise IncompleteInspectError(reading.problems)
    assert reading.exit_code is not None and reading.oom_killed is not None and reading.status is not None
    return EvalContainerState(reading.exit_code, reading.oom_killed, reading.status)


def to_manifest_state(reading: InspectReading, raw_inspect_path: str | PurePath) -> ManifestContainerState:
    """`manifest.ContainerState` with the full container id, memory, inspect time and the five-field excerpt."""
    excerpt = reading.excerpt  # raises IncompleteInspectError
    assert reading.inspect_time is not None
    return ManifestContainerState(
        status=excerpt.status,
        exit_code=excerpt.exit_code,
        oom_killed=excerpt.oom_killed,
        container_id=excerpt.id,
        host_config_memory_bytes=excerpt.memory,
        inspect_time=format_utc_ms(reading.inspect_time),
        raw_inspect_path=posix_path(raw_inspect_path),
        raw_inspect_excerpt=excerpt,
    )


def to_inspect_record(reading: InspectReading, raw_inspect_path: str | PurePath | None) -> InspectRecord:
    """`evaluate.InspectRecord` from whatever the reading holds: a missing field stays None, so `has_all()` is False."""
    return InspectRecord(
        id=reading.id,
        status=reading.status,
        oom_killed=reading.oom_killed,
        exit_code=reading.exit_code,
        memory=reading.memory,
        raw_inspect_path=None if raw_inspect_path is None else posix_path(raw_inspect_path),
        inspect_time=reading.inspect_time,
    )


def to_termination_state(reading: InspectReading) -> dict[str, Any]:
    state = to_eval_state(reading)
    return {"status": state.status, "exit_code": state.exit_code, "oom_killed": state.oom_killed}


def derive_cause_label(experiment: str, state: InspectReading | EvalContainerState | None) -> str:
    """The cause label follows the OBSERVED state, never the experiment name alone; a mismatch gives "unknown"."""
    if isinstance(state, InspectReading):
        if not state.complete:
            return CAUSE_UNKNOWN
        state = to_eval_state(state)
    if not isinstance(state, EvalContainerState) or experiment not in CAUSE_LABELS_BY_EXPERIMENT:
        return CAUSE_UNKNOWN
    if evaluate_container_state(experiment, state).passed:
        return CAUSE_LABELS_BY_EXPERIMENT[experiment]
    return CAUSE_UNKNOWN


def until_exited(reading: InspectReading) -> bool:
    return reading.status == "exited"


def until_running(reading: InspectReading) -> bool:
    return reading.status == "running"


def until_not_running(reading: InspectReading) -> bool:
    return reading.status is not None and reading.status != "running"


# --------------------------------------------------------------------------------------------- context


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ExperimentContext:
    experiment: str
    docker: DockerPort
    collector: CollectorClient
    runs_dir: Path | str
    mem_limit: str | None = DEFAULT_MEM_LIMIT
    leak_chunk_bytes: int = DEFAULT_LEAK_CHUNK_BYTES
    sample_interval_ms: int | None = None
    demo_url: str = DEFAULT_DEMO_URL
    http: Transport | None = None
    wall_clock: Callable[[], datetime] = _utc_now
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    rand_hex: Callable[[], str] | None = None
    # --- state filled in by the preamble (read-only for experiments) ---
    run_id: str | None = None
    run_dir: Path | None = None
    manifest: RunManifest | None = None
    mem_limit_bytes: int | None = None
    container_id: str | None = None
    durability: str | None = None
    clock_offset_ms: int | None = None
    clock_offset_uncertainty_ms: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.experiment, str) or self.experiment.upper() not in EXPECTED_STATE:
            raise ValueError(f"unknown experiment {self.experiment!r} (expected one of A, B, C)")
        self.experiment = self.experiment.upper()
        self.runs_dir = Path(self.runs_dir)
        if self.http is None:
            self.http = urllib_transport()

    def now(self) -> datetime:
        return utc_now_ms(self.wall_clock)

    def stamp(self) -> str:
        return format_utc_ms(self.now())


@dataclass(frozen=True)
class ExperimentResult:
    exit_code: int
    outcome: str  # "passed" | "failed" | "aborted" | "not_started"
    experiment: str
    run_id: str | None
    manifest_path: Path | None
    message: str
    failure: Failure | None = None
    checks: tuple[Check, ...] = ()
    phase: str | None = None

    @property
    def line(self) -> str:
        return f"Experiment {self.experiment} {self.run_id or '-'}: {self.outcome.upper()} - {self.message}"


@dataclass(frozen=True)
class PreambleResult:
    run_id: str
    container_id: str
    clock_offset_ms: int
    clock_offset_uncertainty_ms: float
    durability: str | None
    demo_health: dict[str, Any] | None
    baseline: "EvidenceBundle"
    first_sample: SampleView


def host_time(ctx: ExperimentContext, container_time: datetime) -> datetime:
    """Container-clock time expressed on the host clock: `container_time - clock_offset_ms`."""
    if ctx.clock_offset_ms is None:
        raise RuntimeError("clock offset is not known before the preamble has run")
    return container_time - timedelta(milliseconds=ctx.clock_offset_ms)


# --------------------------------------------------------------------------------------------- manifest


def _new_extra() -> dict[str, Any]:
    return {
        "phase": None,
        "phases": [],
        "finished": False,
        "container_id": None,
        "container_finished_at": None,
        "container_left_running": False,
        "docker_inspect_path": None,
        "container_state_problems": [],
        "collector": None,
        "clock_offset_ms": None,
        "clock_offset_uncertainty_ms": None,
        "memory": None,
        "highest_rss_bytes": None,
        "leak": None,
        "times": {"crash_request_time": None, "sigkill_sent_time": None},
        "collector_health": None,
        "termination": None,
        "checks": [],
        "diagnostics": None,
        "sampling_limitations": None,
        "sample_timing": None,
        "evidence_summary": None,
        "limitations": [UNOBSERVED_NOTE],
    }


def _require_manifest(ctx: ExperimentContext) -> RunManifest:
    if ctx.manifest is None or ctx.run_id is None:
        raise RuntimeError("no run manifest: the preamble has not created a run yet")
    return ctx.manifest


def write_phase(ctx: ExperimentContext, phase: str, *, updates: Mapping[str, Any] | None = None) -> Path:
    """Atomically rewrite the run manifest after a phase (Requirement 12.7)."""
    manifest = _require_manifest(ctx)
    if updates:
        manifest.extra.update(updates)
    now = ctx.stamp()
    manifest.extra["phase"] = phase
    manifest.extra["phases"].append({"name": phase, "time": now})
    manifest.end_time = now
    try:
        return write_manifest(ctx.runs_dir, manifest)
    except (ManifestError, OSError, TypeError, ValueError) as exc:
        raise ManifestWriteError(f"could not write the run manifest in phase {phase!r}: {exc}") from exc


def record_time(ctx: ExperimentContext, key: str, instant: datetime) -> None:
    manifest = _require_manifest(ctx)
    manifest.extra["times"][key] = format_utc_ms(instant)


def checks_to_dicts(checks: Sequence[Check]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for check in checks:
        item: dict[str, Any] = {"name": check.name, "passed": bool(check.passed)}
        if check.detail is not None:
            item["detail"] = check.detail
        out.append(item)
    return out


def failure_for_checks(checks: Sequence[Check], message: str | None = None) -> Failure:
    """The first failed check decides the failure code; the message lists every failed check with its detail."""
    failed = [c for c in checks if not c.passed]
    if not failed:
        return Failure("check_failed", message or "no failed check")
    first = failed[0].name
    code = CHECK_FAILURE_CODES.get(first)
    if code is None:
        code = next((c for prefix, c in CHECK_FAILURE_PREFIXES if first.startswith(prefix)), "check_failed")
    listing = "; ".join(f"{c.name}" + (f" ({c.detail})" if c.detail else "") for c in failed)
    text = f"failed checks: {listing}"
    if message:
        text = f"{message}; {text}"
    return Failure(code, text)


def _fmt(value: datetime | None) -> str | None:
    if value is None:
        return None
    try:
        return format_utc_ms(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------------- finish


def _result_from_manifest(ctx: ExperimentContext, exit_code: int, path: Path | None, message: str) -> ExperimentResult:
    manifest = _require_manifest(ctx)
    checks = tuple(
        Check(c["name"], c["passed"], c.get("detail")) for c in manifest.extra.get("checks", [])
    )
    return ExperimentResult(
        exit_code=exit_code,
        outcome=manifest.outcome,
        experiment=ctx.experiment,
        run_id=ctx.run_id,
        manifest_path=path,
        message=message,
        failure=manifest.failure,
        checks=checks,
        phase=manifest.extra.get("phase"),
    )


def pre_run_result(ctx: ExperimentContext, error: ExperimentError) -> ExperimentResult:
    """Result for a failure before any run id or manifest exists (nothing is written)."""
    return ExperimentResult(
        exit_code=error.exit_code,
        outcome="not_started",
        experiment=ctx.experiment,
        run_id=None,
        manifest_path=None,
        message=error.message,
        failure=error.failure,
    )


def fail_run(ctx: ExperimentContext, error: ExperimentError) -> ExperimentResult:
    """Record `error` in the manifest (keeping everything observed so far) and return the result."""
    if ctx.manifest is None:
        return pre_run_result(ctx, error)
    manifest = ctx.manifest
    manifest.outcome = error.outcome if error.outcome in ("failed", "aborted") else "failed"
    manifest.failure = error.failure
    manifest.extra["finished"] = True
    message = error.message
    try:
        path: Path | None = write_phase(ctx, f"failed:{error.code}")
    except ManifestWriteError as exc:
        path = None
        message = f"{message} (and the manifest could not be written: {exc.message})"
    return _result_from_manifest(ctx, error.exit_code, path, message)


def finalize_run(
    ctx: ExperimentContext, *, evaluation: Evaluation | None = None, extra_checks: Sequence[Check] = ()
) -> ExperimentResult:
    """Write the final manifest. Passed only when `evaluation` passed and every extra check passed."""
    manifest = _require_manifest(ctx)
    checks: list[Check] = list(evaluation.checks) if evaluation is not None else []
    checks.extend(extra_checks)
    manifest.extra["checks"] = checks_to_dicts(checks)
    evaluation_ok = evaluation.passed if evaluation is not None else True
    passed = bool(checks) and evaluation_ok and all(c.passed for c in checks)
    manifest.extra["finished"] = True
    if passed:
        manifest.outcome = "passed"
        manifest.failure = None
        message = evaluation.message if evaluation is not None else "all checks passed"
        exit_code = EXIT_PASSED
        phase = "finished"
    else:
        manifest.outcome = "failed"
        if not checks:
            manifest.failure = Failure("check_failed", "no checks were recorded")
        else:
            manifest.failure = failure_for_checks(
                checks, evaluation.message if evaluation is not None and not evaluation.passed else None
            )
        message = manifest.failure.message
        exit_code = EXIT_FAILED
        phase = "finished"
    try:
        path: Path | None = write_phase(ctx, phase)
    except ManifestWriteError as exc:
        path = None
        message = f"{message} (and the manifest could not be written: {exc.message})"
        exit_code = EXIT_FAILED
    return _result_from_manifest(ctx, exit_code, path, message)


# --------------------------------------------------------------------------------------------- polling


def _wait_until(
    ctx: ExperimentContext, step: Callable[[], tuple[bool, str]], timeout_s: float, interval_s: float
) -> tuple[bool, str, float]:
    """Run `step` until it reports done or `timeout_s` passes (one attempt at the deadline). Uses ctx clock and sleep."""
    start = ctx.monotonic()
    deadline = start + timeout_s
    while True:
        done, detail = step()
        now = ctx.monotonic()
        if done:
            return True, detail, now - start
        remaining = deadline - now
        if remaining <= 0:
            return False, detail, now - start
        ctx.sleep(min(interval_s, remaining))


@dataclass(frozen=True)
class PollResult:
    outcome: str  # "matched" | "timeout" | "inspect_failed"
    reading: InspectReading | None
    result: InspectResult | None
    attempts: int
    elapsed_s: float
    error: str | None = None

    @property
    def matched(self) -> bool:
        return self.outcome == "matched"


def poll_inspect(
    ctx: ExperimentContext,
    container_id: str,
    until: Callable[[InspectReading], bool],
    interval_s: float,
    timeout_s: float,
) -> PollResult:
    """Poll `docker inspect` until `until(reading)` is true for a complete reading, or `timeout_s` passes.

    At the deadline only the LAST attempt decides: an incomplete reading or an inspect error gives
    "inspect_failed" (earlier complete readings do not hide it); a complete reading that did not match `until`
    gives "timeout". Transient failures between good readings are ignored.
    """
    state: dict[str, Any] = {"reading": None, "result": None, "attempts": 0, "error": None, "last_complete": False}

    def step() -> tuple[bool, str]:
        state["attempts"] += 1
        state["last_complete"] = False
        try:
            result = ctx.docker.inspect(container_id)
        except (DockerError, ValueError) as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
            return False, state["error"]
        reading = read_inspect(result)
        state["reading"], state["result"] = reading, result
        if not reading.complete:
            state["error"] = "incomplete inspect data: " + "; ".join(reading.problems)
            return False, state["error"]
        state["last_complete"] = True
        state["error"] = None
        return (True, "matched") if until(reading) else (False, f"status={reading.status}")

    matched, _detail, elapsed = _wait_until(ctx, step, timeout_s, interval_s)
    if matched:
        outcome = "matched"
    elif state["last_complete"]:
        outcome = "timeout"
    else:
        outcome = "inspect_failed"
    return PollResult(outcome, state["reading"], state["result"], state["attempts"], elapsed, state["error"])


def inspect_once(ctx: ExperimentContext, container_id: str) -> InspectReading:
    try:
        return read_inspect(ctx.docker.inspect(container_id))
    except (DockerError, ValueError) as exc:
        raise InspectFailed(f"docker inspect failed: {type(exc).__name__}: {exc}") from exc


def record_inspect(
    ctx: ExperimentContext, reading: InspectReading, *, save_raw: bool = True
) -> ManifestContainerState | None:
    """Save the raw inspect output and record the observed Container_State in the manifest."""
    manifest = _require_manifest(ctx)
    assert ctx.run_dir is not None
    raw_path: str | None = None
    if save_raw and reading.result is not None:
        target = ctx.run_dir / RAW_INSPECT_NAME
        try:
            save_raw_inspect(target, reading.result.raw)
        except (OSError, TypeError) as exc:
            raise InspectFailed(f"could not save the raw docker inspect output: {exc}") from exc
        raw_path = posix_path(target)
    state: ManifestContainerState | None = None
    if reading.complete and raw_path is not None:
        state = to_manifest_state(reading, raw_path)
    manifest.container_state = state
    write_phase(
        ctx,
        "container_inspected",
        updates={
            "docker_inspect_path": raw_path,
            "container_finished_at": _fmt(reading.finished_at),
            "container_state_problems": list(reading.problems),
        },
    )
    return state


# --------------------------------------------------------------------------------------------- Demo_App HTTP


def demo_request(ctx: ExperimentContext, method: str, path: str, *, timeout_s: float) -> HttpResponse:
    assert ctx.http is not None
    body = b"" if method.upper() == "POST" else None
    try:
        return ctx.http(method, ctx.demo_url.rstrip("/") + path, body, timeout_s)
    except (CollectorUnavailable, OSError, ValueError) as exc:
        raise DemoRequestError(f"{type(exc).__name__}: {exc}") from exc


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------------------------- leak driver


@dataclass(frozen=True)
class LeakRequestRecord:
    index: int
    sent_time: datetime
    http_status: int | None
    elapsed_s: float
    ok: bool
    error: str | None
    retained_bytes_total: int | None


@dataclass(frozen=True)
class LeakResult:
    chunk_bytes: int
    requests: tuple[LeakRequestRecord, ...]
    cumulative_bytes: int
    reason: str  # "complete" | "stopped" | "timeout" | "request_failed"
    error: str | None
    first_call_time: datetime | None
    last_response_time: datetime | None
    last_response_mono: float | None
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.reason == "complete"

    @property
    def request_count(self) -> int:
        return len(self.requests)

    def block(self) -> dict[str, Any]:
        return {
            "chunk_bytes": self.chunk_bytes,
            "requests": self.request_count,
            "successful_requests": sum(1 for r in self.requests if r.ok),
            "cumulative_bytes": self.cumulative_bytes,
            "first_call_time": _fmt(self.first_call_time),
            "last_response_time": _fmt(self.last_response_time),
            "duration_s": round(self.duration_s, 3),
            "reason": self.reason,
            "error": self.error,
        }


def drive_leak(
    ctx: ExperimentContext,
    *,
    chunk_bytes: int | None = None,
    min_duration_s: float | None = LEAK_MIN_DURATION_S,
    max_duration_s: float | None = None,
    interval_s: float = LEAK_INTERVAL_S,
    request_timeout_s: float = LEAK_REQUEST_TIMEOUT_S,
    stop_when: Callable[[], bool] | None = None,
    abort_on_failure: bool = True,
) -> LeakResult:
    """Drive POST /leak. See the module docstring (FROZEN INTERFACE) for the exact stop rules."""
    if min_duration_s is None and max_duration_s is None and stop_when is None:
        raise ValueError("drive_leak needs min_duration_s, max_duration_s or stop_when")
    chunk = ctx.leak_chunk_bytes if chunk_bytes is None else chunk_bytes
    if not _is_int(chunk) or not 1 <= chunk <= MAX_LEAK_BYTES:
        raise ValueError(f"chunk_bytes must be an int in 1..{MAX_LEAK_BYTES}, got {chunk!r}")
    records: list[LeakRequestRecord] = []
    cumulative = 0
    first_call: datetime | None = None
    last_time: datetime | None = None
    last_mono: float | None = None
    reason = ""
    error: str | None = None
    t0 = ctx.monotonic()
    index = 0
    while True:
        if stop_when is not None and stop_when():
            reason = "stopped"
            break
        if max_duration_s is not None and ctx.monotonic() - t0 >= max_duration_s:
            reason = "timeout"
            break
        sent = ctx.now()
        if first_call is None:
            first_call = sent
        started = ctx.monotonic()
        status: int | None = None
        total: int | None = None
        problem: str | None = None
        try:
            response = demo_request(ctx, "POST", f"/leak?bytes={chunk}", timeout_s=request_timeout_s)
        except DemoRequestError as exc:
            problem = f"POST /leak failed: {exc}"
        else:
            status = response.status
            body = _json_object(response.body)
            if isinstance(body, dict) and _is_int(body.get("retained_bytes_total")):
                total = body["retained_bytes_total"]
            if status != 200:
                problem = f"POST /leak returned HTTP {status}"
        finished = ctx.monotonic()
        elapsed = finished - started
        if problem is None and elapsed > request_timeout_s:
            problem = f"POST /leak took {elapsed:.3f} s (limit {request_timeout_s:g} s)"
        ok = problem is None
        records.append(LeakRequestRecord(index, sent, status, elapsed, ok, problem, total))
        index += 1
        if not ok:
            reason, error = "request_failed", problem
            break
        cumulative += chunk
        last_time, last_mono = ctx.now(), finished
        if min_duration_s is not None and finished - t0 >= min_duration_s:
            reason = "complete"
            break
        pause = t0 + index * interval_s - ctx.monotonic()
        if pause > 0:
            ctx.sleep(pause)
    result = LeakResult(
        chunk_bytes=chunk,
        requests=tuple(records),
        cumulative_bytes=cumulative,
        reason=reason,
        error=error,
        first_call_time=first_call,
        last_response_time=last_time,
        last_response_mono=last_mono,
        duration_s=ctx.monotonic() - t0,
    )
    failed = reason == "request_failed"
    write_phase(ctx, "leak_failed" if failed else "leak_finished", updates={"leak": result.block()})
    if failed and abort_on_failure:
        raise ExperimentError("leak_failed", f"{error}; aborted before any crash or kill")
    return result


def post_leak_wait_s(interval_ms: int | None) -> float:
    """Seconds to wait after the last leak response so a Sample at least 1 s later exists:
    the larger of the design's max(1 s, 1.5 * interval) and 1 s + interval + 0.25 s."""
    interval = (interval_ms if interval_ms is not None else 500) / 1000.0
    return max(1.0, 1.5 * interval, 1.0 + interval + 0.25)


def wait_after_leak(ctx: ExperimentContext, leak: LeakResult) -> float:
    if leak.last_response_mono is None:
        return 0.0
    remaining = leak.last_response_mono + post_leak_wait_s(ctx.sample_interval_ms) - ctx.monotonic()
    if remaining > 0:
        ctx.sleep(remaining)
        return remaining
    return 0.0


# --------------------------------------------------------------------------------------------- evidence


@dataclass(frozen=True)
class EvidenceBundle:
    raw: bytes
    result: ReadResult
    summary: RunSummary
    samples: tuple[SampleView, ...]
    durability_levels: tuple[str, ...]
    fetched_at: datetime

    @property
    def record_count(self) -> int:
        return len(self.result.records)

    @property
    def sample_count(self) -> int:
        return len(self.samples)


def _sample_views(result: ReadResult) -> tuple[SampleView, ...]:
    views: list[SampleView] = []
    for record in result.records:
        if record.get("record_type") != "sample":
            continue
        try:
            sample = record["sample"]
            views.append(
                SampleView(
                    ts=parse_utc_ms(sample["ts"]),
                    recv_time=parse_utc_ms(record["recv_time"]),
                    rss_bytes=sample.get("rss_bytes"),
                    seq=sample.get("seq"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceUnavailable(f"a stored sample record could not be read: {type(exc).__name__}: {exc}") from exc
    return tuple(sorted(views, key=lambda s: s.ts))


def read_evidence_bundle(ctx: ExperimentContext) -> EvidenceBundle:
    run_id = _require_manifest(ctx).run_id
    try:
        response = ctx.collector.get_evidence(run_id)
    except CollectorUnavailable as exc:
        raise EvidenceUnavailable(f"evidence fetch failed: {exc}") from exc
    if response.status != 200:
        raise EvidenceUnavailable(f"evidence fetch returned HTTP {response.status}", response.status)
    result = read_evidence(response.body, name=f"{run_id}{FILE_SUFFIX}")
    summary = summarize(result)
    return EvidenceBundle(
        raw=response.body,
        result=result,
        summary=summary,
        samples=_sample_views(result),
        durability_levels=tuple(summary.durability_levels),
        fetched_at=ctx.now(),
    )


def _evidence_summary_block(summary: RunSummary) -> dict[str, Any]:
    return {
        "sample_count": summary.sample_count,
        "missing_sample_count": summary.missing_sample_count,
        "unreconciled_drop_count": summary.unreconciled_drop_count,
        "unreconciled_drop_uncertain": summary.unreconciled_drop_uncertain,
        "malformed_line_count": summary.malformed_line_count,
    }


def fetch_evidence(ctx: ExperimentContext) -> EvidenceBundle:
    """Fetch the evidence through the Collector, keep a host copy, and record it in the manifest."""
    manifest = _require_manifest(ctx)
    assert ctx.run_dir is not None
    bundle = read_evidence_bundle(ctx)
    target = ctx.run_dir / EVIDENCE_COPY_NAME
    try:
        atomic_write_bytes(target, bundle.raw)
    except OSError as exc:
        raise EvidenceUnavailable(f"could not save the evidence copy: {exc}") from exc
    manifest.evidence = EvidenceRef(
        container_path=f"{EVIDENCE_CONTAINER_DIR}/{manifest.run_id}{FILE_SUFFIX}",
        host_copy_path=posix_path(target),
        record_count=bundle.record_count,
    )
    write_phase(ctx, "evidence_fetched", updates={"evidence_summary": _evidence_summary_block(bundle.summary)})
    return bundle


# --------------------------------------------------------------------------------------------- termination


@dataclass(frozen=True)
class TerminationPost:
    ok: bool
    http_status: int | None
    cause_label: str
    payload: dict[str, Any] | None
    error: str | None = None

    @property
    def check(self) -> Check:
        return Check("termination_recorded", self.ok, "recorded" if self.ok else self.error)


def post_termination(
    ctx: ExperimentContext, reading: InspectReading, *, cause_label: str | None = None, note: str | None = None
) -> TerminationPost:
    """Ask the Collector to store the observed termination. Collector trouble is returned, not raised."""
    manifest = _require_manifest(ctx)
    label = cause_label if cause_label is not None else derive_cause_label(ctx.experiment, reading)
    post: TerminationPost
    if not reading.complete or reading.inspect_time is None:
        post = TerminationPost(False, None, label, None, "Container_State could not be read: " + "; ".join(reading.problems))
    else:
        payload = {
            "source": TERMINATION_SOURCE,
            "observed_time": format_utc_ms(reading.inspect_time),
            "container_state": to_termination_state(reading),
            "cause_label": label,
            "note": note if note is not None else TERMINATION_NOTE,
        }
        try:
            response = ctx.collector.post_termination(manifest.run_id, payload)
        except CollectorUnavailable as exc:
            post = TerminationPost(False, None, label, payload, f"termination post failed: {exc}")
        else:
            if response.status == 200:
                post = TerminationPost(True, 200, label, payload)
            else:
                post = TerminationPost(False, response.status, label, payload, f"termination post returned HTTP {response.status}")
    write_phase(
        ctx,
        "termination_posted",
        updates={
            "termination": {
                "posted": post.ok,
                "http_status": post.http_status,
                "cause_label": post.cause_label,
                "observed_time": None if post.payload is None else post.payload["observed_time"],
                "error": post.error,
            }
        },
    )
    return post


# --------------------------------------------------------------------------------------------- survival


@dataclass(frozen=True)
class SurvivalResult:
    ok: bool
    health: HealthResult | None
    elapsed_s: float
    detail: str

    @property
    def check(self) -> Check:
        return Check("collector_survival", self.ok, self.detail)


def verify_collector_survival(ctx: ExperimentContext, within_s: float = SURVIVAL_WITHIN_S) -> SurvivalResult:
    """Collector survival: HTTP 200 and `storage_ok is True` within `within_s` seconds."""
    start = ctx.monotonic()
    health: HealthResult | None = None
    ok = False
    late = False
    detail = ""
    while True:
        health = ctx.collector.health()
        elapsed = ctx.monotonic() - start
        if health.ok:
            if elapsed <= within_s:
                ok, detail = True, "http 200 and storage_ok=true"
            else:
                late = True
                detail = f"healthy reply arrived after {elapsed:.3f} s (limit {within_s:g} s)"
            break
        if health.storage_unhealthy:
            detail = f"http {health.http_status} with storage_ok={health.storage_ok!r}"
            break
        if elapsed >= within_s:
            detail = (
                f"no healthy reply within {within_s:g} s (last: "
                + (f"http {health.http_status}" if health.http_status is not None else f"no response, {health.error}")
                + ")"
            )
            break
        ctx.sleep(min(SURVIVAL_POLL_S, within_s - elapsed))
    elapsed = ctx.monotonic() - start
    block = {
        "http_status": health.http_status,
        "storage_ok": health.storage_ok,
        "ok": ok,
        "checked_time": _fmt(health.checked_time),
        "latency_ms": health.latency_ms,
        "elapsed_s": round(elapsed, 3),
        "within_s": within_s,
    }
    returned = replace(health, http_status=None, ok=False, error=detail) if late else health
    write_phase(ctx, "collector_survival", updates={"collector_health": block})
    return SurvivalResult(ok, returned, elapsed, detail)


# --------------------------------------------------------------------------------------------- checks


def first_rss_bytes(samples: Sequence[SampleView]) -> int | None:
    for sample in sorted(samples, key=lambda s: s.ts):
        if sample.rss_bytes is not None:
            return sample.rss_bytes
    return None


def highest_rss_bytes(samples: Sequence[SampleView]) -> int | None:
    values = [s.rss_bytes for s in samples if s.rss_bytes is not None]
    return max(values) if values else None


def record_memory(
    ctx: ExperimentContext,
    samples: Sequence[SampleView],
    *,
    reading: InspectReading | None = None,
    include_block: bool,
) -> None:
    manifest = _require_manifest(ctx)
    highest = highest_rss_bytes(samples)
    manifest.extra["highest_rss_bytes"] = highest
    if include_block:
        manifest.extra["memory"] = {
            "configured_limit_bytes": ctx.mem_limit_bytes,
            "memory_limit_bytes": None if reading is None else reading.memory,
            "highest_rss_bytes": highest,
            "first_rss_bytes": first_rss_bytes(samples),
        }


def memory_limit_check(ctx: ExperimentContext, reading: InspectReading) -> Check:
    name = "memory_limit_matches_host_config"
    if ctx.mem_limit_bytes is None or reading.memory is None:
        return Check(name, False, f"configured {ctx.mem_limit_bytes!r}, HostConfig.Memory {reading.memory!r}")
    return Check(
        name,
        ctx.mem_limit_bytes == reading.memory,
        f"configured {ctx.mem_limit_bytes}, HostConfig.Memory {reading.memory}",
    )


def growth_check(
    samples: Sequence[SampleView],
    leak: LeakResult,
    clock_offset_ms: int,
    *,
    fraction: float = 0.5,
    min_after_s: float = 1.0,
    name: str = "memory_growth_matches_leak",
) -> Check:
    """Requirement 7.4: rss(first Sample captured at least `min_after_s` after the last leak response) minus
    rss(last Sample captured before the first leak request) must be at least `fraction * cumulative_bytes`.
    Capture times are corrected to the host clock first; a null RSS is skipped, never read as 0."""
    if leak.first_call_time is None or leak.last_response_time is None or leak.cumulative_bytes <= 0:
        return Check(name, False, "no successful leak request to compare against")
    offset = timedelta(milliseconds=clock_offset_ms)
    ordered = sorted(samples, key=lambda s: s.ts)
    before = [s for s in ordered if s.rss_bytes is not None and s.ts - offset < leak.first_call_time]
    threshold = leak.last_response_time + timedelta(seconds=min_after_s)
    after = [s for s in ordered if s.rss_bytes is not None and s.ts - offset >= threshold]
    if not before:
        return Check(name, False, "no Sample with a readable RSS before the first leak request")
    if not after:
        return Check(name, False, f"no Sample with a readable RSS captured {min_after_s:g} s or more after the last leak")
    base, later = before[-1].rss_bytes, after[0].rss_bytes
    assert base is not None and later is not None
    growth = later - base
    needed = fraction * leak.cumulative_bytes
    return Check(name, growth >= needed, f"growth {growth} bytes, needed at least {needed:g} (leaked {leak.cumulative_bytes})")


def durability_check(ctx: ExperimentContext, bundle: EvidenceBundle) -> Check:
    """Requirement 4.16: the durability from /healthz equals every `run_metadata` durability in the evidence."""
    name = "durability_matches_run_metadata"
    levels = bundle.durability_levels
    if not levels:
        return Check(name, False, "no run_metadata record in the evidence")
    if ctx.durability is None:
        return Check(name, False, "the Collector did not report a durability level")
    ok = all(level == ctx.durability for level in levels)
    return Check(name, ok, f"healthz {ctx.durability!r}, run_metadata {list(levels)!r}")


def check_no_shutdown_records(bundle: EvidenceBundle) -> Check:
    """Requirement 10.10: only known Collector record types and Probe Samples carrying both `ts` and `recv_time`."""
    name = "no_shutdown_handler_records"
    for record in bundle.result.records:
        kind = record.get("record_type")
        if kind not in _KNOWN_RECORD_TYPES:
            return Check(name, False, f"unexpected record type {kind!r}")
        if kind == "sample":
            sample = record.get("sample")
            if not isinstance(sample, Mapping) or "ts" not in sample or "recv_time" not in record:
                return Check(name, False, "a sample record lacks ts or recv_time")
    return Check(name, True, f"{bundle.record_count} records, all of known types")


def check_sample_captured_before(
    name: str, samples: Sequence[SampleView], instant: datetime, clock_offset_ms: int
) -> Check:
    """At least one Sample whose offset-corrected Capture_Time is earlier than `instant`."""
    offset = timedelta(milliseconds=clock_offset_ms)
    count = sum(1 for s in samples if s.ts - offset < instant)
    return Check(name, count >= 1, f"{count} Samples captured before {format_utc_ms(instant)}")


# --------------------------------------------------------------------------------------------- preamble


def _validate_context(ctx: ExperimentContext) -> None:
    if ctx.mem_limit is not None:
        try:
            ctx.mem_limit_bytes = parse_mem_size(ctx.mem_limit)
        except (ValueError, TypeError) as exc:
            raise PreRunError("invalid_arguments", f"invalid memory limit: {exc}") from exc
    chunk = ctx.leak_chunk_bytes
    if not _is_int(chunk) or not 1 <= chunk <= MAX_LEAK_BYTES:
        raise PreRunError("invalid_arguments", f"invalid leak chunk size {chunk!r} (expected 1 to {MAX_LEAK_BYTES} bytes)")
    interval = ctx.sample_interval_ms
    if interval is not None and not (_is_int(interval) and 100 <= interval <= 60000):
        raise PreRunError("invalid_arguments", f"invalid sample interval {interval!r} (expected 100 to 60000 ms)")


def _require_docker(ctx: ExperimentContext) -> None:
    try:
        available = ctx.docker.available()
    except Exception:  # noqa: BLE001 - any failure of the availability probe means Docker cannot be used
        available = False
    if available is not True:
        raise PreRunError("docker_unavailable", "Docker is unavailable")


def _create_run(ctx: ExperimentContext) -> None:
    assert isinstance(ctx.runs_dir, Path)
    try:
        ctx.runs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreRunError("environment_error", f"cannot create the runs directory {ctx.runs_dir}: {exc}") from exc
    known: set[str] = set()
    for _ in range(RUN_DIR_ATTEMPTS):
        try:
            run_id = generate_run_id(
                ctx.experiment.lower(), known, runs_dir=ctx.runs_dir, clock=ctx.wall_clock, rand_hex=ctx.rand_hex
            )
        except (RunIdError, RunIdExhaustedError) as exc:
            raise PreRunError("environment_error", f"cannot generate a run id: {exc}") from exc
        run_dir = ctx.runs_dir / run_id
        try:
            run_dir.mkdir(exist_ok=False)  # claims the directory atomically; an existing one is never reused
        except FileExistsError:
            known.add(run_id)
            continue
        except OSError as exc:
            raise PreRunError("environment_error", f"cannot create {run_dir}: {exc}") from exc
        ctx.run_id, ctx.run_dir = run_id, run_dir
        break
    else:
        raise PreRunError("environment_error", "no unused run directory found")
    expected = EXPECTED_STATE[ctx.experiment]
    now = ctx.stamp()
    ctx.manifest = RunManifest(
        experiment=ctx.experiment,
        run_id=run_id,
        outcome="aborted",
        failure=Failure(PLACEHOLDER_FAILURE_CODE, "the run has not finished (still running, or interrupted)"),
        start_time=now,
        end_time=now,
        durability_level="unknown",
        termination_description=TERMINATION_DESCRIPTIONS[ctx.experiment],
        expected=Expected(exit_code=expected[0], oom_killed=expected[1]),
        extra=_new_extra(),
    )
    write_phase(ctx, "run_created")


def _check_collector(ctx: ExperimentContext) -> None:
    manifest = _require_manifest(ctx)
    outcome = check_ready(ctx.collector, ctx.monotonic, ctx.sleep, window_s=READINESS_WINDOW_S)
    health = outcome.health
    block: dict[str, Any] = {
        "readiness": {
            "status": outcome.status,
            "message": outcome.message,
            "attempts": outcome.attempts,
            "waited_s": round(outcome.waited_s, 3),
            "http_status": None if health is None else health.http_status,
            "storage_ok": None if health is None else health.storage_ok,
        },
        "durability": None if health is None else health.durability,
        "server_time": None if health is None else health.server_time,
        "evidence_precheck": None,
    }
    manifest.extra["collector"] = block
    if not outcome.ready:
        write_phase(ctx, "collector_not_ready")
        detail = outcome.message
        if outcome.status == "collector_unhealthy" and health is not None and health.error:
            detail = f"{detail} (last error: {health.error})"
        raise ExperimentError(outcome.status, detail)
    assert health is not None
    ctx.durability = health.durability
    manifest.durability_level = health.durability if health.durability else "unknown"
    offset: ClockOffset | None = outcome.clock_offset
    attempts = 0
    while offset is None and attempts < CLOCK_OFFSET_RETRIES:
        attempts += 1
        offset = ctx.collector.health().clock_offset
    if offset is None:
        write_phase(ctx, "collector_ready_no_clock_offset")
        raise ExperimentError(
            "clock_offset_unavailable",
            "the Collector did not report a usable server_time, so the clock offset cannot be estimated",
        )
    ctx.clock_offset_ms = offset.offset_ms
    ctx.clock_offset_uncertainty_ms = offset.uncertainty_ms
    manifest.extra["clock_offset_ms"] = offset.offset_ms
    manifest.extra["clock_offset_uncertainty_ms"] = offset.uncertainty_ms
    # The run id must be new to the Collector too: its evidence file for this id must not exist (404).
    try:
        response = ctx.collector.get_evidence(manifest.run_id)
    except CollectorUnavailable as exc:
        block["evidence_precheck"] = f"unavailable: {exc}"
    else:
        if response.status == 200:
            block["evidence_precheck"] = "exists"
            write_phase(ctx, "run_id_collision")
            raise ExperimentError("run_id_collision", f"the Collector already holds evidence for run id {manifest.run_id}")
        block["evidence_precheck"] = f"http {response.status}"
    write_phase(ctx, "collector_ready")


def _demo_env(ctx: ExperimentContext) -> dict[str, str]:
    assert ctx.run_id is not None
    env = {"CORONER_RUN_ID": ctx.run_id, "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS": "1"}
    if ctx.mem_limit is not None:
        env["CORONER_DEMO_MEM_LIMIT"] = ctx.mem_limit
    if ctx.sample_interval_ms is not None:
        env["CORONER_SAMPLE_INTERVAL_MS"] = str(ctx.sample_interval_ms)
    return env


def _start_demo_app(ctx: ExperimentContext) -> None:
    manifest = _require_manifest(ctx)
    try:
        ctx.docker.compose_up(DEMO_SERVICE, env=_demo_env(ctx), no_deps=True, recreate=True)
    except DockerError as exc:
        write_phase(ctx, "demo_app_start_failed")
        raise ExperimentError("docker_compose_failed", f"docker compose up failed: {exc}") from exc
    try:
        container_id = ctx.docker.container_id(DEMO_SERVICE)
    except DockerOutputError as exc:
        write_phase(ctx, "container_id_failed")
        raise ExperimentError("invalid_container_id", f"invalid container id: {exc}") from exc
    except DockerError as exc:
        write_phase(ctx, "container_id_failed")
        raise ExperimentError("docker_compose_failed", f"docker compose ps failed: {exc}") from exc
    if not (isinstance(container_id, str) and _HEX64.fullmatch(container_id)):
        write_phase(ctx, "container_id_failed")
        raise ExperimentError(
            "invalid_container_id", f"container id {container_id!r} is not 64 lowercase hex characters"
        )
    ctx.container_id = container_id
    manifest.extra["container_id"] = container_id
    write_phase(ctx, "demo_app_started")


def _wait_for_demo_health(ctx: ExperimentContext) -> dict[str, Any] | None:
    seen: dict[str, Any] = {"body": None}

    def step() -> tuple[bool, str]:
        try:
            response = demo_request(ctx, "GET", "/health", timeout_s=DEMO_HEALTH_REQUEST_TIMEOUT_S)
        except DemoRequestError as exc:
            return False, str(exc)
        if response.status != 200:
            return False, f"HTTP {response.status}"
        body = _json_object(response.body)
        run_id = None if body is None else body.get("run_id")
        if isinstance(run_id, str) and run_id != ctx.run_id:
            return False, f"health reports run_id {run_id!r}, not this run (a previous container may still be answering)"
        seen["body"] = body
        return True, "healthy"

    ok, detail, _elapsed = _wait_until(ctx, step, DEMO_HEALTH_TIMEOUT_S, DEMO_HEALTH_POLL_S)
    if not ok:
        write_phase(ctx, "demo_app_unhealthy")
        raise ExperimentError(
            "demo_app_failed_to_start",
            f"Demo_App failed to start: no healthy /health response within {DEMO_HEALTH_TIMEOUT_S:g} s (last: {detail})",
        )
    write_phase(ctx, "demo_app_healthy")
    return seen["body"]


def _wait_for_first_sample(ctx: ExperimentContext) -> EvidenceBundle:
    seen: dict[str, Any] = {"bundle": None}

    def step() -> tuple[bool, str]:
        try:
            bundle = read_evidence_bundle(ctx)
        except EvidenceUnavailable as exc:
            return False, exc.message
        if bundle.sample_count >= 1:
            seen["bundle"] = bundle
            return True, "sample seen"
        return False, "no Sample stored yet"

    ok, detail, _elapsed = _wait_until(ctx, step, FIRST_SAMPLE_TIMEOUT_S, FIRST_SAMPLE_POLL_S)
    if not ok:
        write_phase(ctx, "no_baseline_sample")
        raise ExperimentError(
            "no_baseline_sample",
            f"no Sample was stored within {FIRST_SAMPLE_TIMEOUT_S:g} s of the Demo_App starting (last: {detail})",
        )
    bundle = seen["bundle"]
    write_phase(ctx, "baseline_sample_seen")
    return bundle


def run_preamble(ctx: ExperimentContext) -> PreambleResult:
    """Common preamble (design "Experiment flows"). Raises PreRunError before a run id exists (nothing written),
    ExperimentError afterwards (the manifest already holds everything observed; `fail_run` records the failure)."""
    _validate_context(ctx)
    _require_docker(ctx)
    _create_run(ctx)
    _check_collector(ctx)
    _start_demo_app(ctx)
    health = _wait_for_demo_health(ctx)
    baseline = _wait_for_first_sample(ctx)
    assert ctx.run_id and ctx.container_id and ctx.clock_offset_ms is not None
    assert ctx.clock_offset_uncertainty_ms is not None
    return PreambleResult(
        run_id=ctx.run_id,
        container_id=ctx.container_id,
        clock_offset_ms=ctx.clock_offset_ms,
        clock_offset_uncertainty_ms=ctx.clock_offset_uncertainty_ms,
        durability=ctx.durability,
        demo_health=health,
        baseline=baseline,
        first_sample=baseline.samples[0],
    )


def _unexpected(exc: BaseException) -> ExperimentError:
    return ExperimentError("internal_error", f"unexpected error: {type(exc).__name__}: {exc}")


def run_experiment(
    ctx: ExperimentContext, body: Callable[[ExperimentContext, PreambleResult], ExperimentResult]
) -> ExperimentResult:
    """Preamble, then `body`. Every failure after the run id exists leaves a readable manifest."""
    try:
        pre = run_preamble(ctx)
    except PreRunError as exc:
        return pre_run_result(ctx, exc)
    except ExperimentError as exc:
        return fail_run(ctx, exc)
    except KeyboardInterrupt:
        fail_run(ctx, ExperimentError("interrupted", "interrupted by the user"))
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, never lost
        return fail_run(ctx, _unexpected(exc))
    try:
        return body(ctx, pre)
    except ExperimentError as exc:
        return fail_run(ctx, exc)
    except KeyboardInterrupt:
        fail_run(ctx, ExperimentError("interrupted", "interrupted by the user"))
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, never lost
        return fail_run(ctx, _unexpected(exc))
