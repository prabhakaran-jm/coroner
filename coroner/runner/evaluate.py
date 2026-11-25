"""Pure evaluation for the runner (Requirements 8.4, 9.6, 9.10, 9.11, 10.4, 10.6, 10.8 to 10.13,
11.5, 11.6, 11.10, 11.11, 11.13, 11.14, 11.17 to 11.20).

Part 1: `parse_mem_size` (re-exported from `coroner.common.envparse`), `ContainerState`,
`Evaluation`, `evaluate_container_state`.
Part 2 (Experiment B timing): `SampleView`, `BTiming`, `classify_b_samples`,
`evaluate_experiment_b`, `CLOCK_TOLERANCE_MS`.
Part 3 (Experiment C): `InspectRecord`, `DiagnosticMetrics`, `SamplingLimitations`,
`diagnostic_metrics`, `evaluate_experiment_c`, `sampling_limitations`.

No I/O, no Docker, no clock. All timestamps are timezone-aware `datetime` values (any UTC
offset; they are compared as exact instants). Naive datetimes are rejected with ValueError.
Time arithmetic uses `timedelta`, which is exact to the microsecond, so a Sample captured at
exactly kill + 250 ms is within tolerance and kill + 251 ms is beyond it.

Conversion requirements for the later experiment flow (task 6.15 and 6.16):

* `evaluate.ContainerState` (exit_code, oom_killed, status) and `manifest.ContainerState` are
  different classes and are not unified. The experiment-B and -C evaluators read the state
  through attributes `exit_code` and `oom_killed`, so either class (or None) is accepted.
* Evidence records must be converted to `SampleView` first: `ts` is the parsed Capture_Time,
  `recv_time` the parsed Receive_Time, `rss_bytes` the RSS or None. A null RSS stays None; it
  is never treated as 0.
* `HealthResult` (collector_client) is read through `http_status` and `storage_ok`.
* `InspectRecord` is built from the saved `docker inspect` data (Id, State, HostConfig.Memory),
  the raw inspect file path and the inspect time.

Unit tests over these functions cover the decision logic only. They do not prove that a
genuine Docker OOM kill happened; only a real Docker run does.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from coroner.common.envparse import parse_mem_size
from coroner.common.timeutil import format_utc_ms

if TYPE_CHECKING:  # annotation only; the evaluator reads health through attributes
    from coroner.runner.collector_client import HealthResult

__all__ = [
    "BTiming",
    "CLOCK_TOLERANCE_MS",
    "Check",
    "ContainerState",
    "DiagnosticMetrics",
    "EXPECTED_STATE",
    "EXPECTED_STATUS",
    "Evaluation",
    "InspectRecord",
    "SampleView",
    "SamplingLimitations",
    "classify_b_samples",
    "diagnostic_metrics",
    "evaluate_container_state",
    "evaluate_experiment_b",
    "evaluate_experiment_c",
    "parse_mem_size",
    "sampling_limitations",
]

# experiment -> (exit_code, oom_killed)
EXPECTED_STATE: dict[str, tuple[int, bool]] = {
    "A": (1, False),
    "B": (137, False),
    "C": (137, True),
}
EXPECTED_STATUS = "exited"

# Documented clock-difference tolerance for Experiment B. Never above 250; do not change it
# without asking the user.
CLOCK_TOLERANCE_MS = 250

DIAGNOSTIC_TAIL_SIZE = 5
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ContainerState:
    """Observed Docker `State`: `ExitCode`, `OOMKilled`, `Status`."""

    exit_code: int
    oom_killed: bool
    status: str


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str | None = None


@dataclass(frozen=True)
class Evaluation:
    passed: bool
    message: str
    checks: tuple[Check, ...] = ()


def _observed(state: ContainerState) -> str:
    return (
        f"exit_code={state.exit_code!r} oom_killed={state.oom_killed!r} status={state.status!r}"
    )


def evaluate_container_state(experiment: str, state: ContainerState) -> Evaluation:
    """Pass iff the observed state equals the experiment's expected state.

    Expected: A (1, False), B (137, False), C (137, True), status "exited". Types are
    exact: `exit_code` must be an int and not a bool, `oom_killed` must be a bool, and
    `status` must be the str "exited". A missing or malformed state fails (never raises).
    An unknown experiment identifier raises ValueError naming the value.
    """
    if not isinstance(experiment, str) or experiment not in EXPECTED_STATE:
        raise ValueError(f"unknown experiment {experiment!r} (expected one of A, B, C)")
    want_code, want_oom = EXPECTED_STATE[experiment]
    expected = f"exit_code={want_code!r} oom_killed={want_oom!r} status={EXPECTED_STATUS!r}"
    if not isinstance(state, ContainerState):
        return Evaluation(
            False,
            f"Experiment {experiment}: Container_State could not be read "
            f"(got {state!r}); expected {expected}",
        )
    code, oom, status = state.exit_code, state.oom_killed, state.status
    checks = (
        Check("exit_code", type(code) is int and code == want_code, f"observed {code!r}, expected {want_code!r}"),
        Check("oom_killed", type(oom) is bool and oom is want_oom, f"observed {oom!r}, expected {want_oom!r}"),
        Check("status", type(status) is str and status == EXPECTED_STATUS, f"observed {status!r}, expected {EXPECTED_STATUS!r}"),
    )
    if all(c.passed for c in checks):
        return Evaluation(True, f"Experiment {experiment}: observed {_observed(state)}", checks)
    return Evaluation(
        False,
        f"Experiment {experiment}: unexpected container state, observed {_observed(state)}; "
        f"expected {expected}",
        checks,
    )


# ---------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------


def _require_aware(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got {value!r}")
    return value


def _is_int(value: object) -> bool:
    return type(value) is int


@dataclass(frozen=True)
class SampleView:
    """The fields of one stored Probe Sample that the evaluators read.

    `ts` is Capture_Time (Probe clock) and `recv_time` is Receive_Time (Collector clock); both
    are aware datetimes (naive raises ValueError). `rss_bytes` is None when the RSS was not
    readable, which is never the same as 0. `seq` is an optional identifier for reporting.
    """

    ts: datetime
    recv_time: datetime | None = None
    rss_bytes: int | None = None
    seq: int | None = None

    def __post_init__(self) -> None:
        _require_aware("ts", self.ts)
        if self.recv_time is not None:
            _require_aware("recv_time", self.recv_time)
        if self.rss_bytes is not None:
            if not _is_int(self.rss_bytes):
                raise TypeError(f"rss_bytes must be an int or None, got {self.rss_bytes!r}")
            if self.rss_bytes < 0:
                raise ValueError(f"rss_bytes must not be negative, got {self.rss_bytes!r}")


def _check_samples(samples: Sequence[SampleView]) -> list[SampleView]:
    out = list(samples)
    for s in out:
        if not isinstance(s, SampleView):
            raise TypeError(f"samples must be SampleView items, got {s!r}")
    return out


def _survival(health: Any) -> bool:
    """HTTP 200 and `storage_ok` exactly True (a missing field is unhealthy)."""
    if health is None:
        return False
    status = getattr(health, "http_status", None)
    return _is_int(status) and status == 200 and getattr(health, "storage_ok", None) is True


def _state_field(state: Any, name: str) -> Any:
    return None if state is None else getattr(state, name, None)


def _verdict_message(label: str, checks: tuple[Check, ...]) -> Evaluation:
    failed = [c for c in checks if not c.passed]
    if not failed:
        return Evaluation(True, f"{label}: all {len(checks)} mandatory checks passed", checks)
    names = ", ".join(c.name for c in failed)
    return Evaluation(False, f"{label}: failed mandatory checks: {names}", checks)


# ---------------------------------------------------------------------------------------
# Part 2: Experiment B timing
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BTiming:
    """Result of `classify_b_samples`.

    Every Sample has exactly one primary category: `pre_kill`, `within_tolerance` or
    `post_kill_beyond_tolerance`. `pre_kill_late_received` is a subset of `pre_kill`.
    `after_container_stop` is an independent flag and may overlap any primary category.
    Each tuple is ordered by offset-corrected Capture_Time (stable for ties).
    """

    tolerance_ms: int
    clock_offset_ms: int
    sigkill_sent: datetime
    finished_at: datetime | None
    pre_kill: tuple[SampleView, ...]
    pre_kill_late_received: tuple[SampleView, ...]
    within_tolerance: tuple[SampleView, ...]
    post_kill_beyond_tolerance: tuple[SampleView, ...]
    after_container_stop: tuple[SampleView, ...]

    @property
    def pre_kill_count(self) -> int:
        return len(self.pre_kill)

    @property
    def pre_kill_late_received_count(self) -> int:
        return len(self.pre_kill_late_received)

    @property
    def within_tolerance_count(self) -> int:
        return len(self.within_tolerance)

    @property
    def total_count(self) -> int:
        return len(self.pre_kill) + len(self.within_tolerance) + len(self.post_kill_beyond_tolerance)

    def to_dict(self) -> dict[str, Any]:
        """The manifest `sample_timing` block (anomaly lists hold Capture_Time strings)."""

        def ts_list(items: tuple[SampleView, ...]) -> list[str]:
            return [format_utc_ms(s.ts) for s in items]

        return {
            "tolerance_ms": self.tolerance_ms,
            "clock_offset_ms": self.clock_offset_ms,
            "container_finished_at": None if self.finished_at is None else format_utc_ms(self.finished_at),
            "pre_kill_count": self.pre_kill_count,
            "pre_kill_late_received_count": self.pre_kill_late_received_count,
            "within_tolerance_count": self.within_tolerance_count,
            "post_kill_beyond_tolerance": ts_list(self.post_kill_beyond_tolerance),
            "after_container_stop": ts_list(self.after_container_stop),
        }


def classify_b_samples(
    samples: Sequence[SampleView],
    sigkill_sent: datetime,
    finished_at: datetime | None,
    tolerance_ms: int = CLOCK_TOLERANCE_MS,
    clock_offset_ms: int = 0,
) -> BTiming:
    """Classify Samples by offset-corrected Capture_Time (`ts - clock_offset_ms`) only.

    * `pre_kill`: capture < kill.
    * `within_tolerance`: kill <= capture <= kill + tolerance (inclusive both ends).
    * `post_kill_beyond_tolerance`: capture > kill + tolerance.
    * `pre_kill_late_received` (subset of `pre_kill`): `recv_time` > kill. Receive_Time never
      changes the primary category.
    * `after_container_stop` (independent): capture > `finished_at`, when `finished_at` is given.

    `tolerance_ms` must be an int in 0..250 (never above `CLOCK_TOLERANCE_MS`); `clock_offset_ms`
    must be an int (either sign). Arrival order does not matter. Raises ValueError/TypeError on
    naive datetimes or bad arguments.
    """
    if not _is_int(tolerance_ms) or not 0 <= tolerance_ms <= CLOCK_TOLERANCE_MS:
        raise ValueError(f"tolerance_ms must be an int in 0..{CLOCK_TOLERANCE_MS}, got {tolerance_ms!r}")
    if not _is_int(clock_offset_ms):
        raise ValueError(f"clock_offset_ms must be an int, got {clock_offset_ms!r}")
    kill = _require_aware("sigkill_sent", sigkill_sent)
    if finished_at is not None:
        _require_aware("finished_at", finished_at)
    items = _check_samples(samples)
    offset = timedelta(milliseconds=clock_offset_ms)
    limit = kill + timedelta(milliseconds=tolerance_ms)
    corrected = sorted(((s.ts - offset, s) for s in items), key=lambda pair: pair[0])

    pre: list[SampleView] = []
    late: list[SampleView] = []
    within: list[SampleView] = []
    beyond: list[SampleView] = []
    stopped: list[SampleView] = []
    for capture, s in corrected:
        if capture < kill:
            pre.append(s)
            if s.recv_time is not None and s.recv_time > kill:
                late.append(s)
        elif capture <= limit:
            within.append(s)
        else:
            beyond.append(s)
        if finished_at is not None and capture > finished_at:
            stopped.append(s)
    return BTiming(
        tolerance_ms=tolerance_ms,
        clock_offset_ms=clock_offset_ms,
        sigkill_sent=kill,
        finished_at=finished_at,
        pre_kill=tuple(pre),
        pre_kill_late_received=tuple(late),
        within_tolerance=tuple(within),
        post_kill_beyond_tolerance=tuple(beyond),
        after_container_stop=tuple(stopped),
    )


def evaluate_experiment_b(
    state: ContainerState | None,
    health: HealthResult | None,
    samples: Sequence[SampleView],
    sigkill_sent: datetime,
    finished_at: datetime | None,
    clock_offset_ms: int = 0,
    tolerance_ms: int = CLOCK_TOLERANCE_MS,
    *,
    stopped_within_10s: bool,
    no_shutdown_handler_records: bool,
) -> Evaluation:
    """Experiment B verdict: passed iff all seven mandatory checks pass.

    Checks, in order (names match the manifest): `exit_code_137`, `oom_killed_false`,
    `stopped_within_10s`, `collector_survival`, `at_least_one_sample_captured_before_kill`,
    `no_sample_captured_after_kill_beyond_tolerance`, `no_shutdown_handler_records`.
    `stopped_within_10s` and `no_shutdown_handler_records` are decided by the caller (they need
    the runner clock and the evidence scan) and must be exactly True to pass. Samples captured
    after `finished_at`, late-received pre-kill Samples and within-tolerance Samples are
    reported by `classify_b_samples` but never fail the run by themselves. A missing state
    or health fails its checks (never raises). Use `classify_b_samples` with the same
    arguments for the manifest `sample_timing` block.
    """
    t = classify_b_samples(samples, sigkill_sent, finished_at, tolerance_ms, clock_offset_ms)
    code = _state_field(state, "exit_code")
    oom = _state_field(state, "oom_killed")
    beyond = t.post_kill_beyond_tolerance
    checks = (
        Check("exit_code_137", _is_int(code) and code == 137, f"observed {code!r}, expected 137"),
        Check("oom_killed_false", type(oom) is bool and oom is False, f"observed {oom!r}, expected False"),
        Check("stopped_within_10s", stopped_within_10s is True),
        Check("collector_survival", _survival(health), "http 200 and storage_ok is True required"),
        Check(
            "at_least_one_sample_captured_before_kill",
            t.pre_kill_count >= 1,
            f"{t.pre_kill_count} pre-kill Samples ({t.pre_kill_late_received_count} received after the kill)",
        ),
        Check(
            "no_sample_captured_after_kill_beyond_tolerance",
            len(beyond) == 0,
            f"{len(beyond)} Samples captured more than {t.tolerance_ms} ms after the kill",
        ),
        Check("no_shutdown_handler_records", no_shutdown_handler_records is True),
    )
    return _verdict_message("Experiment B", checks)


# ---------------------------------------------------------------------------------------
# Part 3: Experiment C
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InspectRecord:
    """What the runner recorded from `docker inspect` for Experiment C (Requirements 11.13, 11.14).

    Every field may be None, meaning not recorded. `memory` is `HostConfig.Memory` in bytes.
    """

    id: str | None = None
    status: str | None = None
    oom_killed: bool | None = None
    exit_code: int | None = None
    memory: int | None = None
    raw_inspect_path: str | None = None
    inspect_time: datetime | None = None

    def missing_fields(self) -> tuple[str, ...]:
        """Names of required fields that are absent, mistyped, or empty (empty tuple = complete)."""
        missing: list[str] = []
        if not (isinstance(self.id, str) and _HEX64_RE.fullmatch(self.id)):
            missing.append("Id")
        if not (isinstance(self.status, str) and self.status != ""):
            missing.append("State.Status")
        if type(self.oom_killed) is not bool:
            missing.append("State.OOMKilled")
        if not _is_int(self.exit_code):
            missing.append("State.ExitCode")
        if not (_is_int(self.memory) and self.memory > 0):
            missing.append("HostConfig.Memory")
        if not (isinstance(self.raw_inspect_path, str) and self.raw_inspect_path != ""):
            missing.append("raw_inspect_path")
        t = self.inspect_time
        if not (isinstance(t, datetime) and t.tzinfo is not None and t.utcoffset() is not None):
            missing.append("inspect_time")
        return tuple(missing)

    def has_all(self) -> bool:
        return not self.missing_fields()


@dataclass(frozen=True)
class DiagnosticMetrics:
    """Diagnostic_Metrics (Requirement 11.19). Recorded only; never read by the verdict."""

    rss_monotonic_tail: bool | None  # None: inconclusive (no Samples, or a null RSS in the tail)
    peak_rss_ratio_of_limit: float | None  # None: no readable RSS
    sample_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "note": "informational only; never used for pass or fail",
            "rss_monotonic_tail": self.rss_monotonic_tail,
            "peak_rss_ratio_of_limit": self.peak_rss_ratio_of_limit,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True)
class SamplingLimitations:
    """Sampling_Limitations (Requirement 11.20): the interval, leak rate and three statements."""

    sample_interval_ms: int | None
    approx_leak_rate_bytes_per_s: int | None
    statements: tuple[str, str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_interval_ms": self.sample_interval_ms,
            "approx_leak_rate_bytes_per_s": self.approx_leak_rate_bytes_per_s,
            "statements": list(self.statements),
        }


def _capture_order(samples: Sequence[SampleView]) -> list[SampleView]:
    """Samples sorted by Capture_Time; ties keep their input order."""
    return sorted(_check_samples(samples), key=lambda s: s.ts)


def _readable_rss(ordered: Sequence[SampleView]) -> list[int]:
    return [s.rss_bytes for s in ordered if s.rss_bytes is not None]


def diagnostic_metrics(samples: Sequence[SampleView], limit_bytes: int) -> DiagnosticMetrics:
    """Compute the Diagnostic_Metrics over Samples in capture order.

    `rss_monotonic_tail`: over the last min(5, n) Samples, True when RSS never decreases, False
    when it does, None when there are no Samples or any tail RSS is null.
    `peak_rss_ratio_of_limit`: max non-null RSS / `limit_bytes` (None without a readable RSS).
    `limit_bytes` must be a positive int (ValueError otherwise).
    """
    if not _is_int(limit_bytes) or limit_bytes <= 0:
        raise ValueError(f"limit_bytes must be a positive int, got {limit_bytes!r}")
    ordered = _capture_order(samples)
    tail = ordered[-DIAGNOSTIC_TAIL_SIZE:]
    monotonic: bool | None
    if not tail or any(s.rss_bytes is None for s in tail):
        monotonic = None
    else:
        values = [s.rss_bytes for s in tail]
        monotonic = all(a <= b for a, b in zip(values, values[1:]))  # type: ignore[operator]
    rss = _readable_rss(ordered)
    ratio = max(rss) / limit_bytes if rss else None
    return DiagnosticMetrics(monotonic, ratio, len(ordered))


def evaluate_experiment_c(
    state: ContainerState | None,
    health: HealthResult | None,
    samples: Sequence[SampleView],
    stop_time: datetime | None,
    inspect_record: InspectRecord | None,
    limit_bytes: int,
) -> Evaluation:
    """Experiment C verdict from exactly six mandatory checks; nothing else is consulted.

    1 `oom_killed_true` (`is True`); 2 `exit_code_137` (an int, not a bool); 3
    `collector_survival` (HTTP 200 and `storage_ok is True`); 4 `preserved_precrash_evidence`
    (at least one Sample with `recv_time` before `stop_time`; no `stop_time` fails); 5
    `measurable_memory_growth` (peak non-null RSS > first non-null RSS in capture order; a
    null RSS is skipped, never 0); 6 `docker_inspect_recorded` (`InspectRecord.has_all()`).
    `limit_bytes` and Diagnostic_Metrics are not used. Missing inputs fail checks, never raise
    (except naive datetimes or non-SampleView items, which raise).
    """
    del limit_bytes  # the verdict never reads it (diagnostics only)
    ordered = _capture_order(samples)
    oom = _state_field(state, "oom_killed")
    code = _state_field(state, "exit_code")
    if stop_time is not None:
        _require_aware("stop_time", stop_time)
        precrash = sum(1 for s in ordered if s.recv_time is not None and s.recv_time < stop_time)
    else:
        precrash = 0
    rss = _readable_rss(ordered)
    first_rss = rss[0] if rss else None
    peak_rss = max(rss) if rss else None
    growth = first_rss is not None and peak_rss is not None and peak_rss > first_rss
    missing = inspect_record.missing_fields() if isinstance(inspect_record, InspectRecord) else ("inspect_record",)
    checks = (
        Check("oom_killed_true", oom is True, f"observed {oom!r}, expected True"),
        Check("exit_code_137", _is_int(code) and code == 137, f"observed {code!r}, expected 137"),
        Check("collector_survival", _survival(health), "http 200 and storage_ok is True required"),
        Check("preserved_precrash_evidence", precrash >= 1, f"{precrash} Samples received before stop"),
        Check("measurable_memory_growth", growth, f"peak {peak_rss!r} vs first {first_rss!r}"),
        Check(
            "docker_inspect_recorded",
            not missing,
            "complete" if not missing else "missing or invalid: " + ", ".join(missing),
        ),
    )
    return _verdict_message("Experiment C", checks)


def _human_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    n = float(value)
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    text = f"{int(n)}" if n == int(n) else f"{n:.1f}"
    return f"{text} {units[i]}"


def sampling_limitations(
    sample_interval_ms: int | None = None,
    approx_leak_rate_bytes_per_s: int | None = None,
) -> SamplingLimitations:
    """Build the Sampling_Limitations. Always exactly three statements, whatever the inputs.

    Statement 1 quotes the interval and leak rate (and the growth that fits between two
    Samples); when either is unknown (None or invalid) it says so. Statements 2 and 3 are fixed.
    """
    interval = sample_interval_ms if _is_int(sample_interval_ms) and sample_interval_ms > 0 else None
    rate = approx_leak_rate_bytes_per_s if _is_int(approx_leak_rate_bytes_per_s) and approx_leak_rate_bytes_per_s >= 0 else None
    if interval is not None and rate is not None:
        first = (
            f"Sample_Interval is {interval} ms while the leak adds about {_human_bytes(rate)} per second, "
            f"so up to about {_human_bytes(rate * interval / 1000)} of growth can occur between two Samples."
        )
    else:
        first = (
            "Sample_Interval and the leak rate were not both known, so the growth that can occur "
            "between two Samples cannot be stated."
        )
    statements = (
        first,
        "The final seconds before the out-of-memory kill may be unobserved: the last Sample can predate the kill.",
        "The highest sampled RSS may be lower than the highest RSS the container reached because sampling is not continuous.",
    )
    return SamplingLimitations(interval, rate, statements)
