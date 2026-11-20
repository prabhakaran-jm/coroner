"""The `summary` command: a read-only report built from stored values (task 6.18).

Contract: docs/m0-results/m0.6-runner-cli.md sections 3 and 4. `build_summary` reads `<runs_dir>/<run_id>/manifest.json`
and `<runs_dir>/<run_id>/evidence.jsonl` and nothing else. It never writes, never reads the clock, and never touches
Docker or the network. Anything the stored files do not say is shown as `unknown` or `not recorded`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coroner.evidence.reader import read_evidence
from coroner.evidence.reader import summarize as summarize_evidence
from coroner.runner.manifest import (
    ManifestError,
    RunIdError,
    manifest_path,
    read_manifest,
    validate_run_id_for_path,
)

__all__ = ["Summary", "SummaryError", "build_summary", "render_summary", "summary_exit_code"]

EVIDENCE_FILE_NAME = "evidence.jsonl"
MAX_MALFORMED_DETAIL = 5
LABEL_WIDTH = 16
INDENT = " " * LABEL_WIDTH

DURABILITY_TEXT = {
    "crash": "fsync before acknowledgement; storage caveats apply",
    "process": "no fsync before acknowledgement; evidence may be lost on a host crash",
}
DEFAULT_DURABILITY_TEXT = "storage caveats apply"


class SummaryError(Exception):
    """A condition that makes the summary command exit 2; `str(exc)` is the printed message."""


@dataclass(frozen=True)
class Summary:
    run_id: str
    experiment: str
    outcome: str
    failure_code: str | None
    failure_message: str | None
    sample_count: int
    gap_count: int
    missing_sample_count: int
    unreconciled_drop_count: int
    unreconciled_drop_uncertain: bool
    malformed_line_count: int
    malformed_lines: tuple[tuple[int, int, str], ...]
    last_sample_time: str
    termination_cause: str
    unobserved_interval_note: str | None
    container_status: str | None
    container_exit_code: int | None
    container_oom_killed: bool | None
    container_inspect_time: str | None
    container_left_running: bool
    durability_level: str
    sample_timing: Mapping[str, Any] | None
    diagnostics: Mapping[str, Any] | None
    sampling_limitations: Mapping[str, Any] | None
    warnings: tuple[str, ...]


def _mapping_or_none(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def build_summary(runs_dir: Path | str, run_id: str) -> Summary:
    """Build a Summary from stored files only. Raises SummaryError and nothing else for expected problems."""
    try:
        validate_run_id_for_path(run_id)
    except RunIdError as exc:
        raise SummaryError(f"invalid run id {run_id!r}: {exc}") from exc
    path = manifest_path(runs_dir, run_id)
    try:
        manifest = read_manifest(path)
    except FileNotFoundError as exc:
        raise SummaryError(f"run {run_id} not found under {runs_dir}") from exc
    except OSError as exc:
        raise SummaryError(f"cannot read manifest {path}: {exc}") from exc
    except ManifestError as exc:
        raise SummaryError(f"manifest {path} is not valid: {exc}") from exc
    if manifest.run_id != run_id:
        raise SummaryError(f"manifest {path} belongs to run {manifest.run_id}, not {run_id}")

    evidence_path = Path(runs_dir) / run_id / EVIDENCE_FILE_NAME
    if manifest.evidence is None or not evidence_path.exists():
        code = f", failure {manifest.failure.code}" if manifest.failure is not None else ""
        raise SummaryError(f"no evidence copy for run {run_id} (manifest outcome {manifest.outcome}{code})")
    try:
        result = read_evidence(evidence_path, name=f"{run_id}.jsonl")
    except OSError as exc:
        raise SummaryError(f"cannot read evidence copy {evidence_path}: {exc}") from exc
    if not result.records and result.errors:
        raise SummaryError(
            f"evidence copy for run {run_id} has no valid record ({len(result.errors)} malformed lines)"
        )
    stats = summarize_evidence(result)

    warnings: list[str] = []
    if manifest.evidence.record_count != len(result.records):
        warnings.append(
            f"manifest record_count {manifest.evidence.record_count} differs from {len(result.records)} "
            f"valid records in {manifest.evidence.host_copy_path}"
        )
    for level in stats.durability_levels:
        if level != manifest.durability_level:
            warnings.append(
                f"evidence durability {level!r} differs from manifest durability {manifest.durability_level!r}"
            )
    warnings.extend(stats.warnings)

    state = manifest.container_state
    extra = manifest.extra
    experiment = manifest.experiment.upper()
    return Summary(
        run_id=manifest.run_id,
        experiment=manifest.experiment,
        outcome=manifest.outcome,
        failure_code=manifest.failure.code if manifest.failure is not None else None,
        failure_message=manifest.failure.message if manifest.failure is not None else None,
        sample_count=stats.sample_count,
        gap_count=stats.gap_count,
        missing_sample_count=stats.missing_sample_count,
        unreconciled_drop_count=stats.unreconciled_drop_count,
        unreconciled_drop_uncertain=stats.unreconciled_drop_uncertain,
        malformed_line_count=stats.malformed_line_count,
        malformed_lines=tuple(
            (e.line_no, e.byte_length, e.kind) for e in stats.read_errors[:MAX_MALFORMED_DETAIL]
        ),
        last_sample_time=stats.last_sample_time,
        termination_cause=stats.termination_cause,
        unobserved_interval_note=stats.unobserved_interval_note,
        container_status=state.status if state is not None else None,
        container_exit_code=state.exit_code if state is not None else None,
        container_oom_killed=state.oom_killed if state is not None else None,
        container_inspect_time=state.inspect_time if state is not None else None,
        container_left_running=extra.get("container_left_running") is True,
        durability_level=manifest.durability_level,
        sample_timing=_mapping_or_none(extra.get("sample_timing")) if experiment == "B" else None,
        diagnostics=_mapping_or_none(extra.get("diagnostics")) if experiment == "C" else None,
        sampling_limitations=_mapping_or_none(extra.get("sampling_limitations")) if experiment == "C" else None,
        warnings=tuple(warnings),
    )


def _bool(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def _value(value: object) -> str:
    return "unknown" if value is None else str(value)


def _line(name: str, text: str) -> str:
    return f"{name:<{LABEL_WIDTH}}{text}"


def _timing_lines(timing: Mapping[str, Any] | None) -> list[str]:
    if timing is None:
        return [_line("Sample timing:", "not recorded")]
    beyond = timing.get("post_kill_beyond_tolerance")
    after = timing.get("after_container_stop")
    beyond_list = beyond if isinstance(beyond, list) else []
    after_list = after if isinstance(after, list) else []
    beyond_count = len(beyond_list) if isinstance(beyond, list) else "unknown"
    after_count = len(after_list) if isinstance(after, list) else "unknown"
    lines = [
        _line(
            "Sample timing:",
            f"pre_kill={_value(timing.get('pre_kill_count'))} "
            f"(late_received={_value(timing.get('pre_kill_late_received_count'))}) "
            f"within_tolerance={_value(timing.get('within_tolerance_count'))} "
            f"beyond_tolerance={beyond_count} after_container_stop={after_count} "
            f"tolerance_ms={_value(timing.get('tolerance_ms'))} clock_offset_ms={_value(timing.get('clock_offset_ms'))}",
        )
    ]
    lines.extend(INDENT + f"anomaly beyond_tolerance: {ts}" for ts in beyond_list)
    lines.extend(INDENT + f"anomaly after_container_stop: {ts}" for ts in after_list)
    return lines


def _diagnostics_line(diag: Mapping[str, Any] | None) -> str:
    if diag is None:
        return _line("Diagnostics:", "not recorded")
    ratio = diag.get("peak_rss_ratio_of_limit")
    ratio_text = f"{ratio:.2f}" if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) else "unknown"
    return _line(
        "Diagnostics:",
        f"rss_monotonic_tail={_bool(diag.get('rss_monotonic_tail'))} peak_rss_ratio_of_limit={ratio_text} "
        f"sample_count={_value(diag.get('sample_count'))} (informational, not pass/fail)",
    )


def _limitations_line(limits: Mapping[str, Any] | None) -> str:
    if limits is None:
        return _line("Limitations:", "not recorded")
    interval = limits.get("sample_interval_ms")
    rate = limits.get("approx_leak_rate_bytes_per_s")
    parts = [
        "interval unknown" if interval is None else f"interval {interval} ms",
        "approx leak rate unknown" if rate is None else f"approx leak rate {rate} bytes/s",
    ]
    statements = limits.get("statements")
    if isinstance(statements, list):
        parts.extend(str(s) for s in statements)
    return _line("Limitations:", "; ".join(parts))


def render_summary(summary: Summary) -> str:
    """Pure, ASCII, LF text for a Summary; always ends with a newline."""
    lines = [
        _line("Run:", f"{summary.run_id}  (Experiment {summary.experiment})"),
        _line("Outcome:", summary.outcome),
    ]
    if summary.failure_code is not None:
        lines.append(_line("Failure:", f"{summary.failure_code}: {summary.failure_message}"))
    if summary.container_status is None:
        lines.append(_line("Container:", "not recorded"))
    else:
        lines.append(
            _line(
                "Container:",
                f"status={summary.container_status} exit_code={_value(summary.container_exit_code)} "
                f"oom_killed={_bool(summary.container_oom_killed)} (inspected {_value(summary.container_inspect_time)})",
            )
        )
    if summary.container_left_running:
        lines.append("Container left running: yes")
    lines.append(_line("Samples:", str(summary.sample_count)))
    lines.append(
        _line(
            "Gaps:",
            f"{summary.gap_count} total (missing samples, from sequence discontinuities only: "
            f"{summary.missing_sample_count})",
        )
    )
    lines.append(
        _line(
            "Unreconciled:",
            f"{summary.unreconciled_drop_count} probe-reported drops "
            f"(uncertain: {'yes' if summary.unreconciled_drop_uncertain else 'no'})",
        )
    )
    lines.append(_line("Malformed:", f"{summary.malformed_line_count} lines quarantined (never counted as samples)"))
    for line_no, byte_length, kind in summary.malformed_lines[:MAX_MALFORMED_DETAIL]:
        lines.append(INDENT + f"line {line_no}: {kind}, {byte_length} bytes")
    lines.append(_line("Last sample:", summary.last_sample_time))
    termination = summary.termination_cause
    if summary.unobserved_interval_note is not None:
        termination += f"; {summary.unobserved_interval_note}"
    lines.append(_line("Termination:", termination))
    caveat = DURABILITY_TEXT.get(summary.durability_level, DEFAULT_DURABILITY_TEXT)
    lines.append(_line("Durability:", f"{summary.durability_level} ({caveat})"))
    experiment = summary.experiment.upper()
    if experiment == "B":
        lines.extend(_timing_lines(summary.sample_timing))
    elif experiment == "C":
        lines.append(_diagnostics_line(summary.diagnostics))
        lines.append(_limitations_line(summary.sampling_limitations))
    for warning in summary.warnings:
        lines.append(_line("Warning:", warning))
    text = "\n".join(lines) + "\n"
    return text.encode("ascii", "backslashreplace").decode("ascii")


def summary_exit_code(summary: Summary) -> int:
    """0 for any Summary that was built, whatever the stored outcome (failures are SummaryError, exit 2)."""
    return 0
