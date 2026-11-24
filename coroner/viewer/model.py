"""Internal data types, null-policy helpers, JSON serialisation and allowlist guards
for the Coroner Viewer.

Import constraints: stdlib only, plus coroner.evidence.reader and coroner.runner.manifest
(for type annotations and constants). Never imports coroner.runner.experiments.base,
coroner.runner.evaluate, coroner.runner.docker_port, or any heavy M0 module.

Serialisers ``to_list_item``, ``to_detail``, and ``to_listing`` are added in task 2.9.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# Null-policy helpers
# ---------------------------------------------------------------------------

_JS_INT_MAX = 2 ** 53 - 1  # largest integer representable exactly as a JS number


def safe_int(v: object) -> int | None:
    """Accept a Python ``int`` that is not a ``bool`` and whose absolute value
    is at most 2**53 - 1 (the largest exact JavaScript integer).  Everything
    else returns ``None``.
    """
    if not isinstance(v, int) or isinstance(v, bool):
        return None
    if abs(v) > _JS_INT_MAX:
        return None
    return v


def safe_nonneg_int(v: object) -> int | None:
    """Accept a non-negative ``int`` (not bool) within the JS-safe range."""
    result = safe_int(v)
    if result is None or result < 0:
        return None
    return result


def safe_number(v: object) -> int | float | None:
    """Accept a finite ``int`` or ``float`` that is not a ``bool``.

    Rejects ``bool``, ``NaN``, ``Infinity``, ``-Infinity``, and all other types.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        return v
    return None


def safe_bool(v: object) -> bool | None:
    """Accept only a ``bool``; return ``None`` for everything else."""
    if isinstance(v, bool):
        return v
    return None


def safe_str(v: object) -> str | None:
    """Accept only a ``str``; return ``None`` for everything else.  Never truncates."""
    if isinstance(v, str):
        return v
    return None


# ---------------------------------------------------------------------------
# Capped free-text fields
# ---------------------------------------------------------------------------


class CappedText(NamedTuple):
    """Return value of ``capped()``.

    ``value``           – the (possibly cut) string, or ``None`` when the input is not a str.
    ``truncated``       – ``True`` when ``original_length > cap``; ``None`` when input is not a str.
    ``original_length`` – length in Unicode code points of the original string; ``None`` when not a str.
    """
    value: str | None
    truncated: bool | None
    original_length: int | None


def capped(value: object, cap: int) -> CappedText:
    """Return a :class:`CappedText` triple for one free-text API field.

    *value* must be a ``str`` (the caller is responsible for type-checking first);
    when it is not a ``str`` all three fields are ``None``.  The original string
    is **never modified** – only the ``value`` slot of the returned tuple may differ.
    The cap is in Unicode **code points** (not bytes, not UTF-16 units).
    """
    if not isinstance(value, str):
        return CappedText(None, None, None)
    # Split on code points; str iteration over non-BMP characters is correct in
    # CPython because Python strings are code-point sequences.
    cps = list(value)  # list of code-point characters
    original_length = len(cps)
    if original_length > cap:
        return CappedText("".join(cps[:cap]), True, original_length)
    return CappedText(value, False, original_length)


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------


def dump_json(obj: Any) -> bytes:
    """Serialise *obj* to compact JSON bytes (ASCII-safe, no NaN/Infinity).

    Uses ``ensure_ascii=True`` so U+2028, lone surrogates and all non-ASCII
    characters are \\u-escaped.  Uses ``allow_nan=False`` so a stray ``float('nan')``
    or ``float('inf')`` raises ``ValueError`` rather than producing invalid JSON.
    The compact separator pair ``(",", ":")`` produces the smallest output.
    """
    return json.dumps(
        obj,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")


# ---------------------------------------------------------------------------
# Allowlist guard
# ---------------------------------------------------------------------------


def assert_allowlisted(obj: dict[str, Any], schema: frozenset[str]) -> None:
    """Raise ``AssertionError`` if any key of *obj* is not in *schema*.

    Call this before every ``dump_json`` to surface unexpected fields as a
    500-class bug instead of a leaked field.
    """
    extra = obj.keys() - schema
    if extra:
        raise AssertionError(
            f"response object has undeclared keys: {sorted(extra)}"
        )


# ---------------------------------------------------------------------------
# Internal dataclasses shared by loader.py and the serialisers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactState:
    """Tracks the parsed status and fault details for one artifact of a run.

    ``status`` is one of the six ``ArtifactStatus`` string values.
    ``reason`` is the stable reason code, or ``None`` for ``ok``.
    ``observed_size``    is set only for ``too_large`` (the integer byte count or
                         ``None`` when the size was not measured).
    """
    status: str
    reason: str | None = None
    observed_size: int | None = None  # bytes; non-None only for too_large


@dataclass
class ListFacts:
    """Lightweight per-run facts used to build the overview list.

    Populated by ``loader.py`` from the manifest and evidence summary;
    the full ``RunRecord`` is not retained for every run.
    """
    run_id: str
    # Manifest-derived facts (all None when manifest is unusable)
    experiment: str | None = None
    experiment_type: str | None = None          # "A", "B", "C" or None
    verdict: str = "UNKNOWN"                    # "PASS", "FAIL", "UNKNOWN"
    start_time: str | None = None
    start_time_ms: int | None = None            # epoch ms, for ordering; None when unparseable
    # Docker state facts
    exit_code: int | None = None
    oom_killed: bool | None = None
    docker_state_source: str | None = None
    docker_state_discrepancy: bool | None = None
    # Evidence facts (None when evidence not ok)
    sample_count: int | None = None
    collector_survival: bool | None = None
    # Error / unusable
    reason: str | None = None                   # non-None only when UNKNOWN


@dataclass
class RunRecord:
    """Full parsed data for one run, consumed by the detail serialiser.

    Fields mirror the nine top-level detail blocks.  ``None`` values indicate
    that the data was not available (unusable manifest, missing artifact, etc.).
    """
    run_id: str
    # --- Raw parsed objects from M0 functions ---
    # (types imported lazily below to avoid circular imports; annotated as Any)
    manifest: Any = None                        # RunManifest | None
    manifest_fault: ArtifactState | None = None
    inspect_excerpt: Any = None                 # InspectExcerpt | None
    inspect_fault: ArtifactState | None = None
    read_result: Any = None                     # ReadResult | None
    summary: Any = None                         # RunSummary | None
    evidence_fault: ArtifactState | None = None
    # --- Computed intermediate values ---
    # Filled by classify.py
    experiment_type: str | None = None          # "A", "B", "C" or None
    verdict: str = "UNKNOWN"
    docker_view: Any = None                     # DockerView | None (from classify.py)
    # Filled by samples.py
    sample_rows: list[Any] = field(default_factory=list)   # list[SampleRow]
    rss_stats_data: Any = None                  # RssStats | None
    last_persisted_row: Any = None              # SampleRow | None
    latest_capture_row: Any = None              # SampleRow | None
    # Filled by clocks.py
    offset_resolution: Any = None              # OffsetResolution | None
    # Filled by timeline.py
    chart_data: Any = None                     # Chart | None
    # Computed warnings
    viewer_warnings: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Serialiser allowlists and helpers (task 2.9)
# ---------------------------------------------------------------------------

# Top-level keys of GET /api/runs/{run_id}
DETAIL_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "run",
    "docker_state",
    "timing",
    "termination_markers",
    "evidence_summary",
    "chart",
    "runner_text",
    "artifact_status",
    "warnings",
})

# --- List envelope ---
_LIST_ENVELOPE_KEYS = frozenset({
    "runs_dir_label",
    "total_found",
    "listed",
    "truncated",
    "max_listed",
    "runs",
})

# --- List item ---
_LIST_ITEM_KEYS = frozenset({
    "run_id",
    "experiment",
    "experiment_type",
    "verdict",
    "start_time",
    "start_time_display",
    "exit_code",
    "oom_killed",
    "sample_count",
    "collector_survival",
    "docker_state_source",
    "docker_state_discrepancy",
    "reason",
})

# --- run block ---
_RUN_KEYS = frozenset({
    "run_id",
    "experiment",
    "experiment_type",
    "outcome",
    "verdict",
    "failure",
    "durability_level",
    "start_time", "start_time_display", "start_time_clock",
    "end_time", "end_time_display", "end_time_clock",
    "record_count_manifest",
    "collector_survival",
    "checks",
    "checks_total",
    "checks_truncated",
    "collector_health",
    "termination_record",
    "diagnostics",
    "error",
})

_FAILURE_KEYS = frozenset({
    "code", "code_truncated", "code_original_length",
    "message", "message_truncated", "message_original_length",
})

_CHECK_KEYS = frozenset({
    "name", "name_truncated", "name_original_length",
    "passed",
    "detail", "detail_truncated", "detail_original_length",
})

_COLLECTOR_HEALTH_KEYS = frozenset({
    "http_status", "storage_ok", "ok",
    "checked_time", "checked_time_display",
})

_TERMINATION_RECORD_KEYS = frozenset({
    "posted",
    "observed_time", "observed_time_display",
})

_DIAGNOSTICS_KEYS = frozenset({
    "rss_monotonic_tail",
    "peak_rss_ratio_of_limit",
    "sample_count",
})

_RUN_ERROR_KEYS = frozenset({
    "reason",
})

# --- docker_state block ---
_DOCKER_STATE_KEYS = frozenset({
    "source",
    "raw_unavailable_reason",
    "manifest",
    "raw",
    "effective",
    "fields",
    "discrepancies",
})

_DOCKER_MANIFEST_KEYS = frozenset({
    "present",
    "container_id",
    "status",
    "exit_code",
    "oom_killed",
    "memory_limit_bytes",
    "inspect_time", "inspect_time_display", "inspect_time_clock",
    "finished_at", "finished_at_display", "finished_at_clock",
})

_DOCKER_RAW_KEYS = frozenset({
    "container_id",
    "status",
    "exit_code",
    "oom_killed",
    "memory_limit_bytes",
    "finished_at", "finished_at_display", "finished_at_clock",
})

_DOCKER_EFFECTIVE_KEYS = frozenset({
    "container_id",
    "status",
    "exit_code",
    "oom_killed",
    "memory_limit_bytes",
    "memory_limit_kind",
})

_DOCKER_FIELD_KEYS = frozenset({
    "field", "manifest_value", "raw_value", "result",
})

# --- timing block ---
_TIMING_KEYS = frozenset({
    "start_time", "start_time_display", "start_time_clock",
    "end_time", "end_time_display", "end_time_clock",
    "inspect_time", "inspect_time_display", "inspect_time_clock",
    "crash_request_time", "crash_request_time_display", "crash_request_time_clock",
    "sigkill_sent_time", "sigkill_sent_time_display", "sigkill_sent_time_clock",
    "container_finished_at", "container_finished_at_display", "container_finished_at_clock",
    "state_finished_at", "state_finished_at_display", "state_finished_at_clock",
    "clock_offset_ms",
    "clock_offset_clock",
    "clock_offset_source",
    "clock_offset_conflict",
    "clock_uncertainty_ms",
    "clock_uncertainty_clock",
    "tolerance_ms",
    "tolerance_status",
    "tolerance_clock",
    "time_axis",
    "correction_available",
    "correction_unavailable_reason",
    "sample_timing",
    "derived",
})

_CLOCK_OFFSET_CONFLICT_KEYS = frozenset({
    "sample_timing", "manifest",
})

_SAMPLE_TIMING_KEYS = frozenset({
    "pre_kill_count",
    "pre_kill_late_received_count",
    "within_tolerance_count",
    "post_kill_beyond_tolerance",
    "after_container_stop",
    "after_container_stop_analysis",
    "clock_offset_ms",
    "tolerance_ms",
})

_ST_GROUP_KEYS = frozenset({
    "count",
    "items",
    "items_total",
    "items_truncated",
})

_ST_ITEM_KEYS = frozenset({
    "capture_time", "capture_time_display",
})

_DERIVED_KEYS = frozenset({
    "suppressed",
    "suppressed_reason",
    "finished_at_source",
    "intervals",
})

_DERIVED_INTERVAL_KEYS = frozenset({
    "kind",
    "sample",
    "sample_seq",
    "from_time", "from_time_display", "from_clock",
    "from_basis", "from_corrected_time",
    "to_kind", "to_time", "to_time_display", "to_clock",
    "difference_ms",
    "ordering",
    "uncertainty_ms",
    "tolerance_ms",
    "threshold_ms",
    "bounded",
    "reason",
})

# --- termination_markers block ---
_TERMINATION_MARKERS_KEYS = frozenset({
    "sigkill",
    "text_times",
})

_SIGKILL_MARKER_KEYS = frozenset({
    "drawn", "time", "time_display", "x_ms", "not_drawn_reason",
})

_TEXT_TIME_KEYS = frozenset({
    "kind", "value", "display", "clock",
})

# --- evidence_summary block ---
_EVIDENCE_SUMMARY_KEYS = frozenset({
    "sample_count",
    "valid_record_count",
    "manifest_record_count",
    "record_count_mismatch",
    "missing_sample_count",
    "unreconciled_drop_count",
    "unreconciled_drop_uncertain",
    "unreconciled_drops",
    "unreconciled_drops_total",
    "unreconciled_drops_truncated",
    "silence_intervals",
    "silence_intervals_total",
    "silence_intervals_truncated",
    "malformed_line_count",
    "malformed_lines",
    "malformed_lines_total",
    "malformed_lines_truncated",
    "has_valid_record",
    "first_rss",
    "highest_rss",
    "last_rss",
    "last_persisted_sample",
    "latest_capture_sample",
    "last_receive_time_summary",
    "last_receive_time_summary_display",
    "termination_cause",
    "unobserved_interval_note",
    "durability_levels",
})

_RSS_ENTRY_KEYS = frozenset({
    "seq",
    "rss_bytes",
    "capture_time", "capture_time_display",
    "basis",
})

_LAST_PERSISTED_KEYS = frozenset({
    "seq",
    "capture_time", "capture_time_display",
    "receive_time", "receive_time_display",
    "rss_bytes",
})

_LATEST_CAPTURE_KEYS = frozenset({
    "seq",
    "capture_time", "capture_time_display",
    "rss_bytes",
})

_SILENCE_INTERVAL_KEYS = frozenset({
    "last_sample_time", "last_sample_time_display",
    "detected_time", "detected_time_display",
})

_UNRECONCILED_DROP_KEYS = frozenset({
    "count", "uncertain", "reason",
})

_MALFORMED_LINE_KEYS = frozenset({
    "line_no", "byte_length", "kind",
})

# --- chart block ---
_CHART_KEYS = frozenset({
    "total_samples",
    "plottable_samples",
    "unplottable_samples",
    "plotted_samples",
    "partial_view",
    "chart_cap",
    "time_axis",
    "x_axis",
    "y_data_max_bytes",
    "unknown_range_drop_present",
    "points",
    "gaps",
    "gaps_total",
    "gaps_truncated",
})

_X_AXIS_KEYS = frozenset({
    "domain_min_ms", "domain_max_ms", "ticks",
})

_TICK_KEYS = frozenset({
    "x_ms", "label",
})

_POINT_KEYS = frozenset({
    "seq",
    "capture_time", "capture_time_display",
    "receive_time", "receive_time_display",
    "corrected_capture_time",
    "x_ms",
    "rss_bytes",
    "gap_before",
    "skipped_before",
    "omitted_before",
    "segment_before",
})

_GAP_KEYS = frozenset({
    "gap_type",
    "first_missing_seq",
    "last_missing_seq",
    "range_status",
})

# --- runner_text block ---
_RUNNER_TEXT_KEYS = frozenset({
    "termination_description", "termination_description_truncated", "termination_description_original_length",
    "cause_label", "cause_label_truncated", "cause_label_original_length",
    "limitations",
    "limitations_total",
    "limitations_truncated",
    "sampling_limitations",
})

_LIMITATION_KEYS = frozenset({
    "text", "text_truncated", "text_original_length",
})

_SAMPLING_LIMITATIONS_KEYS = frozenset({
    "sample_interval_ms",
    "approx_leak_rate_bytes_per_s",
    "statements",
    "statements_total",
    "statements_truncated",
})

# --- artifact_status block ---
_ARTIFACT_STATUS_BLOCK_KEYS = frozenset({
    "manifest", "evidence", "inspect",
})

_ARTIFACT_STATUS_ENTRY_KEYS = frozenset({
    "status", "reason", "observed_size_text", "limit_bytes",
})

# --- warnings block ---
_WARNING_KEYS = frozenset({
    "source", "code", "text", "text_truncated", "text_original_length",
})


# ---------------------------------------------------------------------------
# Import of modules needed for serialization
# ---------------------------------------------------------------------------

def _import_viewer_modules() -> None:
    """Deferred imports to avoid circular import at module level."""
    pass  # actual imports are inside the functions that need them


# ---------------------------------------------------------------------------
# Helper: safe type-check helpers for extra fields
# ---------------------------------------------------------------------------

def _safe_type(v: object, typ: type) -> object | None:
    return v if isinstance(v, typ) and not isinstance(v, bool) else None


def _safe_bool_v(v: object) -> bool | None:
    return v if isinstance(v, bool) else None


# ---------------------------------------------------------------------------
# to_list_item
# ---------------------------------------------------------------------------

def to_list_item(facts: "ListFacts") -> dict[str, Any]:
    """Serialise a :class:`ListFacts` to a list-item dict."""
    from coroner.viewer.timefmt import display_utc_ms  # local import

    obj: dict[str, Any] = {
        "run_id": facts.run_id,
        "experiment": safe_str(facts.experiment),
        "experiment_type": safe_str(facts.experiment_type),
        "verdict": facts.verdict,
        "start_time": safe_str(facts.start_time),
        "start_time_display": display_utc_ms(facts.start_time),
        "exit_code": safe_int(facts.exit_code),
        "oom_killed": safe_bool(facts.oom_killed),
        "sample_count": safe_nonneg_int(facts.sample_count),
        "collector_survival": safe_bool(facts.collector_survival),
        "docker_state_source": safe_str(facts.docker_state_source),
        "docker_state_discrepancy": safe_bool(facts.docker_state_discrepancy),
        "reason": safe_str(facts.reason),
    }
    assert_allowlisted(obj, _LIST_ITEM_KEYS)
    return obj


# ---------------------------------------------------------------------------
# to_detail helpers
# ---------------------------------------------------------------------------

def _ser_failure(failure_raw: object) -> dict[str, Any] | None:
    if not isinstance(failure_raw, dict):
        return None
    code_cap = capped(failure_raw.get("code"), 200)
    msg_cap = capped(failure_raw.get("message"), 2000)
    obj: dict[str, Any] = {
        "code": code_cap.value,
        "code_truncated": code_cap.truncated,
        "code_original_length": code_cap.original_length,
        "message": msg_cap.value,
        "message_truncated": msg_cap.truncated,
        "message_original_length": msg_cap.original_length,
    }
    assert_allowlisted(obj, _FAILURE_KEYS)
    return obj


def _ser_check(check: object) -> dict[str, Any] | None:
    if not isinstance(check, dict):
        return None
    name_cap = capped(check.get("name"), 200)
    detail_cap = capped(check.get("detail"), 2000)
    passed = check.get("passed")
    obj: dict[str, Any] = {
        "name": name_cap.value,
        "name_truncated": name_cap.truncated,
        "name_original_length": name_cap.original_length,
        "passed": safe_bool(passed),
        "detail": detail_cap.value,
        "detail_truncated": detail_cap.truncated,
        "detail_original_length": detail_cap.original_length,
    }
    assert_allowlisted(obj, _CHECK_KEYS)
    return obj


def _ser_checks(extra: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, int | None, bool | None]:
    checks_raw = extra.get("checks")
    if checks_raw is None or not isinstance(checks_raw, list):
        return None, None, None
    cap_n = 50
    items = [_ser_check(c) for c in checks_raw if c is not None]
    items = [i for i in items if i is not None]
    total = len(items)
    truncated_b = total > cap_n
    return items[:cap_n], total, truncated_b


def _ser_collector_health(extra: dict[str, Any]) -> dict[str, Any] | None:
    from coroner.viewer.timefmt import display_utc_ms  # local
    block = extra.get("collector_health")
    if not isinstance(block, dict):
        return None
    obj: dict[str, Any] = {
        "http_status": safe_int(block.get("http_status")),
        "storage_ok": safe_bool(block.get("storage_ok")),
        "ok": safe_bool(block.get("ok")),
        "checked_time": safe_str(block.get("checked_time")),
        "checked_time_display": display_utc_ms(block.get("checked_time")),
    }
    assert_allowlisted(obj, _COLLECTOR_HEALTH_KEYS)
    return obj


def _ser_termination_record(extra: dict[str, Any]) -> dict[str, Any] | None:
    from coroner.viewer.timefmt import display_utc_ms  # local
    block = extra.get("termination")
    if not isinstance(block, dict):
        return None
    obj: dict[str, Any] = {
        "posted": safe_bool(block.get("posted")),
        "observed_time": safe_str(block.get("observed_time")),
        "observed_time_display": display_utc_ms(block.get("observed_time")),
    }
    assert_allowlisted(obj, _TERMINATION_RECORD_KEYS)
    return obj


def _ser_diagnostics(extra: dict[str, Any], exp_type: str | None) -> dict[str, Any] | None:
    # diagnostics is null for A and B
    if exp_type in ("A", "B"):
        return None
    block = extra.get("diagnostics")
    if not isinstance(block, dict):
        return None
    obj: dict[str, Any] = {
        "rss_monotonic_tail": safe_bool(block.get("rss_monotonic_tail")),
        "peak_rss_ratio_of_limit": safe_number(block.get("peak_rss_ratio_of_limit")),
        "sample_count": safe_nonneg_int(block.get("sample_count")),
    }
    assert_allowlisted(obj, _DIAGNOSTICS_KEYS)
    return obj


def _ser_run_block(record: "RunRecord") -> dict[str, Any]:
    from coroner.viewer.timefmt import display_utc_ms  # local
    m = record.manifest
    extra: dict[str, Any] = {}
    if m and m.extra:
        extra = m.extra if isinstance(m.extra, dict) else {}

    # failure
    failure_obj: dict[str, Any] | None = None
    if m and m.failure:
        # RunManifest.failure is a dict or object with code/message
        f = m.failure
        if isinstance(f, dict):
            failure_obj = _ser_failure(f)
        elif hasattr(f, "code") or hasattr(f, "message"):
            failure_obj = _ser_failure({
                "code": getattr(f, "code", None),
                "message": getattr(f, "message", None),
            })

    # checks
    checks_list, checks_total, checks_trunc = _ser_checks(extra)

    # collector_survival from checks
    collector_survival: bool | None = None
    checks_raw = extra.get("checks")
    if isinstance(checks_raw, list):
        for check in checks_raw:
            if isinstance(check, dict) and check.get("name") == "collector_survival":
                val = check.get("passed")
                if isinstance(val, bool):
                    collector_survival = val
                break

    # record_count_manifest
    record_count_manifest: int | None = None
    if m and m.evidence is not None:
        record_count_manifest = safe_nonneg_int(m.evidence.record_count)

    # error block (when manifest is unusable)
    error_obj: dict[str, Any] | None = None
    if record.manifest_fault and record.manifest_fault.status != "ok":
        err_reason = record.manifest_fault.reason or record.manifest_fault.status
        err: dict[str, Any] = {"reason": err_reason}
        assert_allowlisted(err, _RUN_ERROR_KEYS)
        error_obj = err

    obj: dict[str, Any] = {
        "run_id": record.run_id,
        "experiment": safe_str(m.experiment if m else None),
        "experiment_type": safe_str(record.experiment_type),
        "outcome": safe_str(m.outcome if m else None),
        "verdict": record.verdict,
        "failure": failure_obj,
        "durability_level": safe_str(m.durability_level if m else None),
        "start_time": safe_str(m.start_time if m else None),
        "start_time_display": display_utc_ms(m.start_time if m else None),
        "start_time_clock": "host",
        "end_time": safe_str(m.end_time if m else None),
        "end_time_display": display_utc_ms(m.end_time if m else None),
        "end_time_clock": "host",
        "record_count_manifest": record_count_manifest,
        "collector_survival": collector_survival,
        "checks": checks_list,
        "checks_total": checks_total,
        "checks_truncated": checks_trunc,
        "collector_health": _ser_collector_health(extra),
        "termination_record": _ser_termination_record(extra),
        "diagnostics": _ser_diagnostics(extra, record.experiment_type),
        "error": error_obj,
    }
    assert_allowlisted(obj, _RUN_KEYS)
    return obj


def _ser_docker_manifest_block(mblock: object) -> dict[str, Any] | None:
    from coroner.viewer.timefmt import display_utc_ms  # local
    if mblock is None:
        return None
    obj: dict[str, Any] = {
        "present": safe_bool(mblock.present) if hasattr(mblock, "present") else False,  # type: ignore[union-attr]
        "container_id": safe_str(mblock.container_id) if hasattr(mblock, "container_id") else None,  # type: ignore[union-attr]
        "status": safe_str(mblock.status) if hasattr(mblock, "status") else None,  # type: ignore[union-attr]
        "exit_code": safe_int(mblock.exit_code) if hasattr(mblock, "exit_code") else None,  # type: ignore[union-attr]
        "oom_killed": safe_bool(mblock.oom_killed) if hasattr(mblock, "oom_killed") else None,  # type: ignore[union-attr]
        "memory_limit_bytes": safe_int(mblock.memory_limit_bytes) if hasattr(mblock, "memory_limit_bytes") else None,  # type: ignore[union-attr]
        "inspect_time": safe_str(mblock.inspect_time) if hasattr(mblock, "inspect_time") else None,  # type: ignore[union-attr]
        "inspect_time_display": display_utc_ms(mblock.inspect_time if hasattr(mblock, "inspect_time") else None),  # type: ignore[union-attr]
        "inspect_time_clock": "host",
        "finished_at": safe_str(mblock.finished_at) if hasattr(mblock, "finished_at") else None,  # type: ignore[union-attr]
        "finished_at_display": display_utc_ms(mblock.finished_at if hasattr(mblock, "finished_at") else None),  # type: ignore[union-attr]
        "finished_at_clock": "docker",
    }
    assert_allowlisted(obj, _DOCKER_MANIFEST_KEYS)
    return obj


def _ser_docker_raw_block(rblock: object) -> dict[str, Any] | None:
    from coroner.viewer.timefmt import display_docker  # local
    if rblock is None:
        return None
    fa_str = rblock.finished_at if hasattr(rblock, "finished_at") else None  # type: ignore[union-attr]
    obj: dict[str, Any] = {
        "container_id": safe_str(rblock.container_id) if hasattr(rblock, "container_id") else None,  # type: ignore[union-attr]
        "status": safe_str(rblock.status) if hasattr(rblock, "status") else None,  # type: ignore[union-attr]
        "exit_code": safe_int(rblock.exit_code) if hasattr(rblock, "exit_code") else None,  # type: ignore[union-attr]
        "oom_killed": safe_bool(rblock.oom_killed) if hasattr(rblock, "oom_killed") else None,  # type: ignore[union-attr]
        "memory_limit_bytes": safe_int(rblock.memory_limit_bytes) if hasattr(rblock, "memory_limit_bytes") else None,  # type: ignore[union-attr]
        # finished_at: return None for zero value (already handled by DockerRawBlock)
        "finished_at": safe_str(fa_str),
        "finished_at_display": display_docker(fa_str),
        "finished_at_clock": "docker",
    }
    assert_allowlisted(obj, _DOCKER_RAW_KEYS)
    return obj


def _ser_docker_effective(eff: object) -> dict[str, Any] | None:
    if eff is None:
        return None
    obj: dict[str, Any] = {
        "container_id": safe_str(eff.container_id) if hasattr(eff, "container_id") else None,  # type: ignore[union-attr]
        "status": safe_str(eff.status) if hasattr(eff, "status") else None,  # type: ignore[union-attr]
        "exit_code": safe_int(eff.exit_code) if hasattr(eff, "exit_code") else None,  # type: ignore[union-attr]
        "oom_killed": safe_bool(eff.oom_killed) if hasattr(eff, "oom_killed") else None,  # type: ignore[union-attr]
        "memory_limit_bytes": safe_int(eff.memory_limit_bytes) if hasattr(eff, "memory_limit_bytes") else None,  # type: ignore[union-attr]
        "memory_limit_kind": safe_str(eff.memory_limit_kind) if hasattr(eff, "memory_limit_kind") else None,  # type: ignore[union-attr]
    }
    assert_allowlisted(obj, _DOCKER_EFFECTIVE_KEYS)
    return obj


def _ser_docker_field(fr: object) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "field": safe_str(fr.field) if hasattr(fr, "field") else None,  # type: ignore[union-attr]
        "manifest_value": fr.manifest_value if hasattr(fr, "manifest_value") else None,  # type: ignore[union-attr]
        "raw_value": fr.raw_value if hasattr(fr, "raw_value") else None,  # type: ignore[union-attr]
        "result": safe_str(fr.result) if hasattr(fr, "result") else None,  # type: ignore[union-attr]
    }
    assert_allowlisted(obj, _DOCKER_FIELD_KEYS)
    return obj


def _ser_docker_state_block(record: "RunRecord") -> dict[str, Any]:
    dv = record.docker_view
    if dv is None:
        from coroner.viewer.classify import DockerView  # local
        dv = DockerView(source="unavailable")

    obj: dict[str, Any] = {
        "source": safe_str(dv.source),
        "raw_unavailable_reason": safe_str(dv.raw_unavailable_reason),
        "manifest": _ser_docker_manifest_block(dv.manifest),
        "raw": _ser_docker_raw_block(dv.raw),
        "effective": _ser_docker_effective(dv.effective),
        "fields": [_ser_docker_field(f) for f in (dv.fields or [])],
        "discrepancies": [_ser_docker_field(f) for f in (dv.discrepancies or [])],
    }
    assert_allowlisted(obj, _DOCKER_STATE_KEYS)
    return obj


def _ser_st_items(items_raw: object, cap: int = 50) -> tuple[list[dict[str, Any]], int, bool]:
    from coroner.viewer.timefmt import display_utc_ms  # local
    if not isinstance(items_raw, list):
        return [], 0, False
    out = []
    for item in items_raw:
        # M0 records plain Capture_Time strings; the {"capture_time": ...} object item is also accepted.
        if isinstance(item, str):
            ct: object = item
        elif isinstance(item, dict):
            ct = item.get("capture_time")
        else:
            continue
        entry: dict[str, Any] = {
            "capture_time": safe_str(ct),
            "capture_time_display": display_utc_ms(ct),
        }
        assert_allowlisted(entry, _ST_ITEM_KEYS)
        out.append(entry)
    total = len(out)
    truncated_b = total > cap
    return out[:cap], total, truncated_b


def _st_group_list(raw: object) -> list[Any] | None:
    """The recorded list of an anomaly group: the M0 plain list, or the `items` list of an object; else None."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        return raw["items"]
    return None


def _ser_sample_timing(extra: dict[str, Any], exp_type: str | None) -> dict[str, Any] | None:
    if exp_type != "B":
        return None
    block = extra.get("sample_timing")
    if not isinstance(block, dict):
        return None

    # post_kill_beyond_tolerance. M0 writes a plain list of Capture_Time strings; an object {"items": [...]} is
    # also accepted. count = len(recorded list); absent or not a list = not recorded = null; [] = 0.
    pkt_list_raw = _st_group_list(block.get("post_kill_beyond_tolerance"))
    pkt_items, pkt_total, pkt_trunc = _ser_st_items(pkt_list_raw)
    pkt_count = len(pkt_list_raw) if pkt_list_raw is not None else None
    pkt_obj: dict[str, Any] = {
        "count": pkt_count,
        "items": pkt_items,
        "items_total": pkt_total,
        "items_truncated": pkt_trunc,
    }
    assert_allowlisted(pkt_obj, _ST_GROUP_KEYS)

    # after_container_stop
    acs_list_raw = _st_group_list(block.get("after_container_stop"))
    acs_items, acs_total, acs_trunc = _ser_st_items(acs_list_raw)

    # count is null when after_container_stop_analysis is "unavailable"
    aca = block.get("after_container_stop_analysis")
    if aca == "unavailable":
        acs_count = None
    else:
        acs_count = len(acs_list_raw) if acs_list_raw is not None else None
    acs_obj: dict[str, Any] = {
        "count": acs_count,
        "items": acs_items,
        "items_total": acs_total,
        "items_truncated": acs_trunc,
    }
    assert_allowlisted(acs_obj, _ST_GROUP_KEYS)

    obj: dict[str, Any] = {
        "pre_kill_count": safe_nonneg_int(block.get("pre_kill_count")),
        "pre_kill_late_received_count": safe_nonneg_int(block.get("pre_kill_late_received_count")),
        "within_tolerance_count": safe_nonneg_int(block.get("within_tolerance_count")),
        "post_kill_beyond_tolerance": pkt_obj,
        "after_container_stop": acs_obj,
        "after_container_stop_analysis": safe_str(block.get("after_container_stop_analysis")),
        "clock_offset_ms": safe_int(block.get("clock_offset_ms")),
        "tolerance_ms": block.get("tolerance_ms"),  # returned as recorded
    }
    assert_allowlisted(obj, _SAMPLE_TIMING_KEYS)
    return obj


def _ser_derived_interval(di: object) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "kind": safe_str(di.kind) if hasattr(di, "kind") else None,  # type: ignore[union-attr]
        "sample": safe_str(di.sample) if hasattr(di, "sample") else None,  # type: ignore[union-attr]
        "sample_seq": safe_int(di.sample_seq) if hasattr(di, "sample_seq") else None,  # type: ignore[union-attr]
        "from_time": safe_str(di.from_time) if hasattr(di, "from_time") else None,  # type: ignore[union-attr]
        "from_time_display": safe_str(di.from_time_display) if hasattr(di, "from_time_display") else None,  # type: ignore[union-attr]
        "from_clock": safe_str(di.from_clock) if hasattr(di, "from_clock") else "container",  # type: ignore[union-attr]
        "from_basis": safe_str(di.from_basis) if hasattr(di, "from_basis") else None,  # type: ignore[union-attr]
        "from_corrected_time": safe_str(di.from_corrected_time) if hasattr(di, "from_corrected_time") else None,  # type: ignore[union-attr]
        "to_kind": safe_str(di.to_kind) if hasattr(di, "to_kind") else None,  # type: ignore[union-attr]
        "to_time": safe_str(di.to_time) if hasattr(di, "to_time") else None,  # type: ignore[union-attr]
        "to_time_display": safe_str(di.to_time_display) if hasattr(di, "to_time_display") else None,  # type: ignore[union-attr]
        "to_clock": safe_str(di.to_clock) if hasattr(di, "to_clock") else "host",  # type: ignore[union-attr]
        "difference_ms": safe_int(di.difference_ms) if hasattr(di, "difference_ms") else None,  # type: ignore[union-attr]
        "ordering": safe_str(di.ordering) if hasattr(di, "ordering") else None,  # type: ignore[union-attr]
        "uncertainty_ms": safe_number(di.uncertainty_ms) if hasattr(di, "uncertainty_ms") else None,  # type: ignore[union-attr]
        "tolerance_ms": di.tolerance_ms if hasattr(di, "tolerance_ms") else None,  # returned as recorded  # type: ignore[union-attr]
        "threshold_ms": safe_number(di.threshold_ms) if hasattr(di, "threshold_ms") else None,  # type: ignore[union-attr]
        "bounded": bool(di.bounded) if hasattr(di, "bounded") else False,  # type: ignore[union-attr]
        "reason": safe_str(di.reason) if hasattr(di, "reason") else None,  # type: ignore[union-attr]
    }
    assert_allowlisted(obj, _DERIVED_INTERVAL_KEYS)
    return obj


def _ser_derived_block(db: object) -> dict[str, Any]:
    if db is None:
        return {
            "suppressed": False,
            "suppressed_reason": None,
            "finished_at_source": None,
            "intervals": [],
        }
    intervals = [_ser_derived_interval(di) for di in (db.intervals or [])]  # type: ignore[union-attr]
    obj: dict[str, Any] = {
        "suppressed": bool(db.suppressed) if hasattr(db, "suppressed") else False,  # type: ignore[union-attr]
        "suppressed_reason": safe_str(db.suppressed_reason) if hasattr(db, "suppressed_reason") else None,  # type: ignore[union-attr]
        "finished_at_source": safe_str(db.finished_at_source) if hasattr(db, "finished_at_source") else None,  # type: ignore[union-attr]
        "intervals": intervals,
    }
    assert_allowlisted(obj, _DERIVED_KEYS)
    return obj


def _ser_timing_block(record: "RunRecord") -> dict[str, Any]:
    from coroner.viewer.timefmt import display_utc_ms, display_docker  # local
    from coroner.viewer.clocks import (  # local
        tolerance_status as get_tol_status,
        axis_mode,
        derived_intervals,
        resolve_offset,
        OffsetResolution,
    )
    m = record.manifest
    extra: dict[str, Any] = {}
    if m and m.extra:
        extra = m.extra if isinstance(m.extra, dict) else {}

    times = extra.get("times") if extra else None
    if not isinstance(times, dict):
        times = {}

    # container_state.inspect_time is in the manifest's container_state
    inspect_time: str | None = None
    if m and m.container_state:
        inspect_time = safe_str(getattr(m.container_state, "inspect_time", None))

    # state_finished_at comes from the raw inspect (docker_inspect.json)
    state_fa: str | None = None
    if record.docker_view and record.docker_view.raw:
        state_fa = record.docker_view.raw.finished_at

    # container_finished_at comes from extra.container_finished_at
    container_fa = safe_str(extra.get("container_finished_at"))

    # Offset resolution (from record if available)
    res = record.offset_resolution
    if res is None:
        res = resolve_offset(record.experiment_type, extra)

    # tolerance_ms (from sample_timing, returned as recorded, never clamped)
    sample_timing = extra.get("sample_timing")
    tol_recorded: object = None
    if isinstance(sample_timing, dict):
        tol_recorded = sample_timing.get("tolerance_ms")
    tol_status = get_tol_status(tol_recorded)

    # axis mode
    mode, _axis_reason = axis_mode(record.experiment_type, res)

    # correction_available / unavailable_reason
    correction_available: bool
    correction_unavailable_reason: str | None = None
    if record.experiment_type == "B":
        if res is not None and res.offset_ms is not None:
            correction_available = True
        elif res is not None and res.conflict is not None:
            correction_available = False
            correction_unavailable_reason = "clock_offsets_conflict"
        else:
            correction_available = False
            correction_unavailable_reason = "offset_not_recorded"
    else:
        correction_available = False

    # Also check if chart reported correction_out_of_range
    chart = record.chart_data
    if chart is not None and chart.correction_unavailable_reason == "correction_out_of_range":
        correction_available = False
        correction_unavailable_reason = "correction_out_of_range"
        mode = "raw_uncorrected"

    # clock_offset_conflict
    conflict_obj: dict[str, Any] | None = None
    if res is not None and res.conflict is not None:
        cv = {
            "sample_timing": res.conflict[0],
            "manifest": res.conflict[1],
        }
        assert_allowlisted(cv, _CLOCK_OFFSET_CONFLICT_KEYS)
        conflict_obj = cv

    # Derived intervals. An out-of-range correction is unusable, so the
    # intervals follow the "B without correction" rule (no corrected sample times).
    res_for_intervals = res
    if correction_unavailable_reason == "correction_out_of_range" and res is not None:
        res_for_intervals = OffsetResolution(offset_ms=None, source=res.source, conflict=None)
    db = derived_intervals(
        record.experiment_type,
        res_for_intervals if res_for_intervals is not None else None,  # type: ignore[arg-type]
        last_persisted_row=record.last_persisted_row,
        latest_capture_row=record.latest_capture_row,
        sigkill_ms=_epoch_ms_times(times, "sigkill_sent_time"),
        finished_at_ms=_epoch_ms_docker_or_utc(
            extra.get("container_finished_at"),
            state_fa,
        ),
        uncertainty_ms=safe_number(extra.get("clock_offset_uncertainty_ms")),
        tolerance_ms_val=tol_recorded,
        tolerance_st=tol_status,
        finished_at_source=_get_finished_at_source(
            extra.get("container_finished_at"),
            state_fa,
        ),
    )

    obj: dict[str, Any] = {
        "start_time": safe_str(m.start_time if m else None),
        "start_time_display": display_utc_ms(m.start_time if m else None),
        "start_time_clock": "host",
        "end_time": safe_str(m.end_time if m else None),
        "end_time_display": display_utc_ms(m.end_time if m else None),
        "end_time_clock": "host",
        "inspect_time": inspect_time,
        "inspect_time_display": display_utc_ms(inspect_time),
        "inspect_time_clock": "host",
        "crash_request_time": safe_str(times.get("crash_request_time")),
        "crash_request_time_display": display_utc_ms(times.get("crash_request_time")),
        "crash_request_time_clock": "host",
        "sigkill_sent_time": safe_str(times.get("sigkill_sent_time")),
        "sigkill_sent_time_display": display_utc_ms(times.get("sigkill_sent_time")),
        "sigkill_sent_time_clock": "host",
        "container_finished_at": container_fa,
        "container_finished_at_display": display_utc_ms(container_fa),
        "container_finished_at_clock": "docker",
        "state_finished_at": safe_str(state_fa),
        "state_finished_at_display": display_docker(state_fa),
        "state_finished_at_clock": "docker",
        "clock_offset_ms": safe_int(res.offset_ms) if res else None,
        "clock_offset_clock": "collector_minus_host",
        "clock_offset_source": safe_str(res.source) if res else None,
        "clock_offset_conflict": conflict_obj,
        "clock_uncertainty_ms": safe_number(extra.get("clock_offset_uncertainty_ms")),
        "clock_uncertainty_clock": "collector_minus_host",
        "tolerance_ms": tol_recorded,  # returned as recorded
        "tolerance_status": tol_status,
        "tolerance_clock": "host",
        "time_axis": mode,
        "correction_available": correction_available,
        "correction_unavailable_reason": correction_unavailable_reason,
        "sample_timing": _ser_sample_timing(extra, record.experiment_type),
        "derived": _ser_derived_block(db),
    }
    assert_allowlisted(obj, _TIMING_KEYS)
    return obj


def _epoch_ms_times(times: dict[str, Any], key: str) -> int | None:
    from coroner.viewer.timefmt import epoch_ms_utc  # local
    return epoch_ms_utc(times.get(key))


def _epoch_ms_docker_or_utc(manifest_fa: object, raw_fa: object) -> int | None:
    """Get epoch ms for FinishedAt: manifest (utc parse) first, then raw (docker parse)."""
    from coroner.viewer.timefmt import epoch_ms_utc, epoch_ms_docker  # local
    ms = epoch_ms_utc(manifest_fa)
    if ms is None:
        ms = epoch_ms_docker(raw_fa)
    return ms


def _get_finished_at_source(manifest_fa: object, raw_fa: object) -> str | None:
    """Return 'manifest' if manifest FinishedAt is parseable, else 'raw_inspect' if raw is."""
    from coroner.viewer.timefmt import epoch_ms_utc, epoch_ms_docker  # local
    if epoch_ms_utc(manifest_fa) is not None:
        return "manifest"
    if epoch_ms_docker(raw_fa) is not None:
        return "raw_inspect"
    return None


def _ser_termination_markers_block(record: "RunRecord") -> dict[str, Any]:
    from coroner.viewer.timefmt import display_utc_ms, display_docker, epoch_ms_utc  # local
    m = record.manifest
    extra: dict[str, Any] = {}
    if m and m.extra:
        extra = m.extra if isinstance(m.extra, dict) else {}

    times = extra.get("times")
    if not isinstance(times, dict):
        times = {}

    exp_type = record.experiment_type
    res = record.offset_resolution

    # SIGKILL marker: drawn only for B with usable offset and recorded sigkill_sent_time
    sigkill_time_str = safe_str(times.get("sigkill_sent_time"))
    sigkill_ms: int | None = epoch_ms_utc(sigkill_time_str) if sigkill_time_str else None

    drawn = False
    not_drawn_reason: str | None = None
    x_ms: int | None = None

    if exp_type != "B":
        not_drawn_reason = "not_experiment_b"
    elif res is None or res.offset_ms is None:
        if res is not None and res.conflict is not None:
            not_drawn_reason = "offset_conflict"
        else:
            not_drawn_reason = "offset_not_recorded"
    elif (
        record.chart_data is not None
        and record.chart_data.correction_unavailable_reason == "correction_out_of_range"
    ):
        # The chart fell back to the raw axis; no corrected position exists.
        not_drawn_reason = "correction_out_of_range"
    elif sigkill_ms is None:
        not_drawn_reason = "sigkill_time_not_recorded"
    else:
        drawn = True
        x_ms = sigkill_ms

    sigkill_obj: dict[str, Any] = {
        "drawn": drawn,
        "time": sigkill_time_str,
        "time_display": display_utc_ms(sigkill_time_str),
        "x_ms": x_ms,
        "not_drawn_reason": not_drawn_reason,
    }
    assert_allowlisted(sigkill_obj, _SIGKILL_MARKER_KEYS)

    # text_times
    text_times: list[dict[str, Any]] = []

    # state_finished_at (docker, A/B/C)
    dv = record.docker_view
    state_fa: str | None = None
    if dv and dv.raw:
        state_fa = dv.raw.finished_at

    def _tt(kind: str, value: str | None, disp: str | None, clock: str) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "kind": kind, "value": value, "display": disp, "clock": clock,
        }
        assert_allowlisted(obj, _TEXT_TIME_KEYS)
        return obj

    # crash_requested: A only
    if exp_type == "A":
        crt = safe_str(times.get("crash_request_time"))
        text_times.append(_tt("crash_requested", crt, display_utc_ms(crt), "host"))

    # sigkill_sent: B only
    if exp_type == "B":
        text_times.append(_tt("sigkill_sent", sigkill_time_str, display_utc_ms(sigkill_time_str), "host"))

    # docker_finished_at: A, B, C
    text_times.append(_tt(
        "docker_finished_at",
        safe_str(state_fa),
        display_docker(state_fa),
        "docker",
    ))

    # last_persisted_capture, latest_capture (container)
    lpr = record.last_persisted_row
    lp_ts = safe_str(lpr.ts) if lpr else None
    text_times.append(_tt("last_persisted_capture", lp_ts, display_utc_ms(lp_ts), "container"))

    lcr = record.latest_capture_row
    lc_ts = safe_str(lcr.ts) if lcr else None
    text_times.append(_tt("latest_capture", lc_ts, display_utc_ms(lc_ts), "container"))

    # last_persisted_receive (collector)
    summary = record.summary
    last_recv: str | None = None
    if summary is not None:
        last_recv = safe_str(getattr(summary, "last_sample_time", None))
    text_times.append(_tt("last_persisted_receive", last_recv, display_utc_ms(last_recv), "collector"))

    obj: dict[str, Any] = {
        "sigkill": sigkill_obj,
        "text_times": text_times,
    }
    assert_allowlisted(obj, _TERMINATION_MARKERS_KEYS)
    return obj


def _ser_evidence_summary_block(record: "RunRecord") -> dict[str, Any]:
    from coroner.viewer.timefmt import display_utc_ms  # local
    from coroner.evidence.reader import UNOBSERVED_NOTE as _NOTE  # local

    summary = record.summary
    result = record.read_result
    ev_ok = (
        record.evidence_fault is not None
        and record.evidence_fault.status == "ok"
        and summary is not None
        and result is not None
    )

    if not ev_ok:
        # Everything null
        null_obj: dict[str, Any] = {
            "sample_count": None,
            "valid_record_count": None,
            "manifest_record_count": None,
            "record_count_mismatch": None,
            "missing_sample_count": None,
            "unreconciled_drop_count": None,
            "unreconciled_drop_uncertain": None,
            "unreconciled_drops": None,
            "unreconciled_drops_total": None,
            "unreconciled_drops_truncated": None,
            "silence_intervals": None,
            "silence_intervals_total": None,
            "silence_intervals_truncated": None,
            "malformed_line_count": None,
            "malformed_lines": None,
            "malformed_lines_total": None,
            "malformed_lines_truncated": None,
            "has_valid_record": None,
            "first_rss": None,
            "highest_rss": None,
            "last_rss": None,
            "last_persisted_sample": None,
            "latest_capture_sample": None,
            "last_receive_time_summary": None,
            "last_receive_time_summary_display": None,
            "termination_cause": None,
            "unobserved_interval_note": None,
            "durability_levels": None,
        }
        assert_allowlisted(null_obj, _EVIDENCE_SUMMARY_KEYS)
        return null_obj

    # Unreconciled drops
    ud_raw = getattr(summary, "unreconciled_drops", []) or []
    ud_cap = 50
    ud_items = []
    for ud in ud_raw:
        if isinstance(ud, dict):
            entry: dict[str, Any] = {
                "count": safe_nonneg_int(ud.get("count")),
                "uncertain": safe_bool(ud.get("uncertain")),
                "reason": safe_str(ud.get("reason")),
            }
        else:
            entry = {
                "count": safe_nonneg_int(getattr(ud, "count", None)),
                "uncertain": safe_bool(getattr(ud, "uncertain", None)),
                "reason": safe_str(getattr(ud, "reason", None)),
            }
        assert_allowlisted(entry, _UNRECONCILED_DROP_KEYS)
        ud_items.append(entry)
    ud_total = len(ud_items)
    ud_trunc = ud_total > ud_cap

    # Silence intervals
    si_raw = getattr(summary, "silence_intervals", []) or []
    si_cap = 50
    si_items = []
    for si in si_raw:
        if isinstance(si, dict):
            lst = si.get("last_sample_time")
            det = si.get("detected_time")
        else:
            lst = getattr(si, "last_sample_time", None)
            det = getattr(si, "detected_time", None)
        s_entry: dict[str, Any] = {
            "last_sample_time": safe_str(lst),
            "last_sample_time_display": display_utc_ms(lst),
            "detected_time": safe_str(det),
            "detected_time_display": display_utc_ms(det),
        }
        assert_allowlisted(s_entry, _SILENCE_INTERVAL_KEYS)
        si_items.append(s_entry)
    si_total = len(si_items)
    si_trunc = si_total > si_cap

    # Malformed lines (from read_errors)
    re_raw = getattr(summary, "read_errors", []) or []
    ml_cap = 20
    ml_items = []
    for re in re_raw:
        if isinstance(re, dict):
            ml_e: dict[str, Any] = {
                "line_no": safe_nonneg_int(re.get("line_no")),
                "byte_length": safe_nonneg_int(re.get("byte_length")),
                "kind": safe_str(re.get("kind")),
            }
        else:
            ml_e = {
                "line_no": safe_nonneg_int(getattr(re, "line_no", None)),
                "byte_length": safe_nonneg_int(getattr(re, "byte_length", None)),
                "kind": safe_str(getattr(re, "kind", None)),
            }
        assert_allowlisted(ml_e, _MALFORMED_LINE_KEYS)
        ml_items.append(ml_e)
    ml_total = len(ml_items)
    ml_trunc = ml_total > ml_cap

    # RSS stats (from record.rss_stats_data)
    stats = record.rss_stats_data

    def _ser_rss_entry(row: object, basis: str | None = None) -> dict[str, Any] | None:
        if row is None:
            return None
        ts_val = safe_str(row.ts) if hasattr(row, "ts") else None  # type: ignore[union-attr]
        entry: dict[str, Any] = {
            "seq": safe_nonneg_int(row.seq) if hasattr(row, "seq") else None,  # type: ignore[union-attr]
            "rss_bytes": safe_nonneg_int(row.rss) if hasattr(row, "rss") else None,  # type: ignore[union-attr]
            "capture_time": ts_val,
            "capture_time_display": display_utc_ms(ts_val),
            "basis": safe_str(basis),
        }
        assert_allowlisted(entry, _RSS_ENTRY_KEYS)
        return entry

    first_rss = _ser_rss_entry(stats.first if stats else None)
    highest_rss = _ser_rss_entry(stats.highest if stats else None)
    last_rss = _ser_rss_entry(stats.last if stats else None, basis=stats.basis if stats else None)

    # last_persisted_sample
    lpr = record.last_persisted_row
    lps_obj: dict[str, Any] | None = None
    if lpr is not None:
        lp_ts = safe_str(lpr.ts)
        lp_recv = safe_str(lpr.recv)
        lps_obj = {
            "seq": safe_nonneg_int(lpr.seq),
            "capture_time": lp_ts,
            "capture_time_display": display_utc_ms(lp_ts),
            "receive_time": lp_recv,
            "receive_time_display": display_utc_ms(lp_recv),
            "rss_bytes": safe_nonneg_int(lpr.rss),
        }
        assert_allowlisted(lps_obj, _LAST_PERSISTED_KEYS)

    # latest_capture_sample
    lcr = record.latest_capture_row
    lcs_obj: dict[str, Any] | None = None
    if lcr is not None:
        lc_ts = safe_str(lcr.ts)
        lcs_obj = {
            "seq": safe_nonneg_int(lcr.seq),
            "capture_time": lc_ts,
            "capture_time_display": display_utc_ms(lc_ts),
            "rss_bytes": safe_nonneg_int(lcr.rss),
        }
        assert_allowlisted(lcs_obj, _LATEST_CAPTURE_KEYS)

    # last_receive_time_summary
    lrt = getattr(summary, "last_sample_time", None)
    lrt_str = safe_str(lrt)
    # "none received" display is null
    lrt_disp = display_utc_ms(lrt_str) if lrt_str and lrt_str != "none received" else None

    # manifest_record_count and mismatch
    m = record.manifest
    manifest_rc: int | None = None
    if m and m.evidence is not None:
        manifest_rc = safe_nonneg_int(m.evidence.record_count)

    valid_rc = len(result.records) if result is not None else None
    mismatch: bool | None = None
    if manifest_rc is not None and valid_rc is not None:
        mismatch = manifest_rc != valid_rc

    obj: dict[str, Any] = {
        "sample_count": safe_nonneg_int(getattr(summary, "sample_count", None)),
        "valid_record_count": safe_nonneg_int(valid_rc),
        "manifest_record_count": manifest_rc,
        "record_count_mismatch": mismatch,
        "missing_sample_count": safe_nonneg_int(getattr(summary, "missing_sample_count", None)),
        "unreconciled_drop_count": safe_nonneg_int(getattr(summary, "unreconciled_drop_count", None)),
        "unreconciled_drop_uncertain": safe_bool(getattr(summary, "unreconciled_drop_uncertain", None)),
        "unreconciled_drops": ud_items[:ud_cap],
        "unreconciled_drops_total": ud_total,
        "unreconciled_drops_truncated": ud_trunc,
        "silence_intervals": si_items[:si_cap],
        "silence_intervals_total": si_total,
        "silence_intervals_truncated": si_trunc,
        "malformed_line_count": safe_nonneg_int(getattr(summary, "malformed_line_count", None)),
        "malformed_lines": ml_items[:ml_cap],
        "malformed_lines_total": ml_total,
        "malformed_lines_truncated": ml_trunc,
        "has_valid_record": valid_rc is not None and valid_rc > 0 if valid_rc is not None else None,
        "first_rss": first_rss,
        "highest_rss": highest_rss,
        "last_rss": last_rss,
        "last_persisted_sample": lps_obj,
        "latest_capture_sample": lcs_obj,
        "last_receive_time_summary": lrt_str,
        "last_receive_time_summary_display": lrt_disp,
        "termination_cause": safe_str(getattr(summary, "termination_cause", None)),
        "unobserved_interval_note": _NOTE,
        "durability_levels": getattr(summary, "durability_levels", None),
    }
    assert_allowlisted(obj, _EVIDENCE_SUMMARY_KEYS)
    return obj


def _ser_chart_block(record: "RunRecord") -> dict[str, Any]:
    chart = record.chart_data
    ev_ok = (
        record.evidence_fault is not None
        and record.evidence_fault.status == "ok"
    )

    if not ev_ok or chart is None:
        obj: dict[str, Any] = {
            "total_samples": None,
            "plottable_samples": None,
            "unplottable_samples": None,
            "plotted_samples": 0,
            "partial_view": False,
            "chart_cap": 5000,
            "time_axis": "raw_capture",
            "x_axis": None,
            "y_data_max_bytes": None,
            "unknown_range_drop_present": False,
            "points": [],
            "gaps": [],
            "gaps_total": 0,
            "gaps_truncated": False,
        }
        assert_allowlisted(obj, _CHART_KEYS)
        return obj

    # x_axis
    x_axis_obj: dict[str, Any] | None = None
    xad = chart.x_axis_data
    if xad is not None:
        ticks = []
        tick_vals = list(xad.tick_values_ms or [])
        tick_lbls = list(xad.tick_labels or [])
        for i, tv in enumerate(tick_vals):
            tick_obj: dict[str, Any] = {
                "x_ms": safe_int(tv),
                "label": safe_str(tick_lbls[i]) if i < len(tick_lbls) else None,
            }
            assert_allowlisted(tick_obj, _TICK_KEYS)
            ticks.append(tick_obj)
        x_axis_obj = {
            "domain_min_ms": safe_int(xad.domain_min_ms),
            "domain_max_ms": safe_int(xad.domain_max_ms),
            "ticks": ticks,
        }
        assert_allowlisted(x_axis_obj, _X_AXIS_KEYS)

    # points
    points_out = []
    from coroner.viewer.timefmt import display_utc_ms  # local
    # file_pos indexes ReadResult.records (which includes non-sample records),
    # not record.sample_rows, so map it explicitly.
    rows_by_file_pos = {r.file_pos: r for r in (record.sample_rows or [])}
    for p in chart.points:
        pt: dict[str, Any] = {
            "seq": safe_nonneg_int(p.seq),
            "capture_time": safe_str(p.ts),
            "capture_time_display": display_utc_ms(p.ts),
            "receive_time": safe_str(p.recv),
            "receive_time_display": display_utc_ms(p.recv),
            "corrected_capture_time": safe_str(p.corrected_capture_time),
            "x_ms": safe_int(p.x_ms),
            "rss_bytes": None,  # will be filled below
            "gap_before": bool(p.gap_before),
            "skipped_before": p.skipped_before,
            "omitted_before": p.omitted_before,
            "segment_before": p.segment_before,
        }
        # rss_bytes: from the original SampleRow (not rss_mib)
        # Find the row by file_pos from record.sample_rows
        rss_b: int | None = None
        row = rows_by_file_pos.get(p.file_pos)
        if row is not None:
            rss_b = safe_nonneg_int(row.rss)
        pt["rss_bytes"] = rss_b
        assert_allowlisted(pt, _POINT_KEYS)
        points_out.append(pt)

    # gaps
    gaps_cap = 100
    gaps_raw = chart.gaps or []
    gaps_total = len(gaps_raw)
    gaps_trunc = gaps_total > gaps_cap
    gaps_out = []
    for g in gaps_raw[:gaps_cap]:
        if isinstance(g, dict):
            g_entry: dict[str, Any] = {
                "gap_type": safe_str(g.get("gap_type")),
                "first_missing_seq": safe_int(g.get("first_missing_seq")),
                "last_missing_seq": safe_int(g.get("last_missing_seq")),
                "range_status": safe_str(g.get("range_status")),
            }
        else:
            g_entry = {
                "gap_type": safe_str(getattr(g, "gap_type", None)),
                "first_missing_seq": safe_int(getattr(g, "first_missing_seq", None)),
                "last_missing_seq": safe_int(getattr(g, "last_missing_seq", None)),
                "range_status": safe_str(getattr(g, "range_status", None)),
            }
        assert_allowlisted(g_entry, _GAP_KEYS)
        gaps_out.append(g_entry)

    obj2: dict[str, Any] = {
        "total_samples": safe_nonneg_int(chart.total_samples),
        "plottable_samples": safe_nonneg_int(chart.plottable_samples),
        "unplottable_samples": safe_nonneg_int(chart.unplottable_samples),
        "plotted_samples": len(points_out),
        "partial_view": bool(chart.partial_view),
        "chart_cap": chart.chart_cap,
        "time_axis": chart.time_axis,
        "x_axis": x_axis_obj,
        "y_data_max_bytes": safe_nonneg_int(chart.y_data_max_bytes),
        "unknown_range_drop_present": bool(chart.unknown_range_drop_present),
        "points": points_out,
        "gaps": gaps_out,
        "gaps_total": gaps_total,
        "gaps_truncated": gaps_trunc,
    }
    assert_allowlisted(obj2, _CHART_KEYS)
    return obj2


def _ser_runner_text_block(record: "RunRecord") -> dict[str, Any]:
    m = record.manifest
    extra: dict[str, Any] = {}
    if m and m.extra:
        extra = m.extra if isinstance(m.extra, dict) else {}

    # termination_description: core manifest field
    td_raw = m.termination_description if m else None
    td = capped(td_raw, 2000)

    # cause_label from extra.termination.cause_label
    term_block = extra.get("termination")
    cause_raw = None
    if isinstance(term_block, dict):
        cause_raw = term_block.get("cause_label")
    cl = capped(cause_raw, 200)

    # limitations from extra.limitations
    lims_raw = extra.get("limitations")
    lims_cap = 50
    lims_out: list[dict[str, Any]] = []
    if isinstance(lims_raw, list):
        for lim in lims_raw:
            lim_text = lim if isinstance(lim, str) else (lim.get("text") if isinstance(lim, dict) else None)
            lim_cap = capped(lim_text, 2000)
            entry: dict[str, Any] = {
                "text": lim_cap.value,
                "text_truncated": lim_cap.truncated,
                "text_original_length": lim_cap.original_length,
            }
            assert_allowlisted(entry, _LIMITATION_KEYS)
            lims_out.append(entry)
    lims_total = len(lims_out)
    lims_trunc = lims_total > lims_cap

    # sampling_limitations: null for A and B
    sl_obj: dict[str, Any] | None = None
    exp_type = record.experiment_type
    if exp_type == "C" or exp_type is None:
        sl_raw = extra.get("sampling_limitations")
        if isinstance(sl_raw, dict):
            stmts_raw = sl_raw.get("statements")
            stmts_cap = 50
            stmts_out: list[dict[str, Any]] = []
            if isinstance(stmts_raw, list):
                for stmt in stmts_raw:
                    s_text = stmt if isinstance(stmt, str) else (stmt.get("text") if isinstance(stmt, dict) else None)
                    s_cap = capped(s_text, 2000)
                    s_entry: dict[str, Any] = {
                        "text": s_cap.value,
                        "text_truncated": s_cap.truncated,
                        "text_original_length": s_cap.original_length,
                    }
                    assert_allowlisted(s_entry, _LIMITATION_KEYS)
                    stmts_out.append(s_entry)
            stmts_total = len(stmts_out)
            stmts_trunc = stmts_total > stmts_cap
            sl_obj = {
                "sample_interval_ms": safe_number(sl_raw.get("sample_interval_ms")),
                "approx_leak_rate_bytes_per_s": safe_number(sl_raw.get("approx_leak_rate_bytes_per_s")),
                "statements": stmts_out[:stmts_cap],
                "statements_total": stmts_total,
                "statements_truncated": stmts_trunc,
            }
            assert_allowlisted(sl_obj, _SAMPLING_LIMITATIONS_KEYS)

    obj: dict[str, Any] = {
        "termination_description": td.value,
        "termination_description_truncated": td.truncated,
        "termination_description_original_length": td.original_length,
        "cause_label": cl.value,
        "cause_label_truncated": cl.truncated,
        "cause_label_original_length": cl.original_length,
        "limitations": lims_out[:lims_cap],
        "limitations_total": lims_total,
        "limitations_truncated": lims_trunc,
        "sampling_limitations": sl_obj,
    }
    assert_allowlisted(obj, _RUNNER_TEXT_KEYS)
    return obj


def _ser_artifact_status_block(record: "RunRecord") -> dict[str, Any]:
    def _one(state: "ArtifactState | None") -> dict[str, Any]:
        if state is None:
            # Default to ok when not set (should not happen)
            e: dict[str, Any] = {
                "status": "ok",
                "reason": None,
                "observed_size_text": None,
                "limit_bytes": None,
            }
            assert_allowlisted(e, _ARTIFACT_STATUS_ENTRY_KEYS)
            return e

        observed_size_text: str | None = None
        limit_bytes: int | None = None
        if state.status == "too_large":
            observed_size_text = "more than 16,777,216 bytes"
            limit_bytes = 16_777_216

        e2: dict[str, Any] = {
            "status": state.status,
            "reason": state.reason,
            "observed_size_text": observed_size_text,
            "limit_bytes": limit_bytes,
        }
        assert_allowlisted(e2, _ARTIFACT_STATUS_ENTRY_KEYS)
        return e2

    obj: dict[str, Any] = {
        "manifest": _one(record.manifest_fault),
        "evidence": _one(record.evidence_fault),
        "inspect": _one(record.inspect_fault),
    }
    assert_allowlisted(obj, _ARTIFACT_STATUS_BLOCK_KEYS)
    return obj


def _ser_warnings_block(record: "RunRecord") -> list[dict[str, Any]]:
    """Build the warnings list from summarize warnings + viewer warnings."""
    summary = record.summary
    warnings_out: list[dict[str, Any]] = []

    # summarize warnings (capped at 50)
    summarize_warnings_cap = 50
    if summary is not None:
        sum_warnings_raw = getattr(summary, "warnings", []) or []
        for w in sum_warnings_raw[:summarize_warnings_cap]:
            w_text_raw = w if isinstance(w, str) else (w.get("text") if isinstance(w, dict) else str(w))
            w_cap = capped(w_text_raw, 2000)
            entry: dict[str, Any] = {
                "source": "summarize",
                "code": None,
                "text": w_cap.value,
                "text_truncated": w_cap.truncated,
                "text_original_length": w_cap.original_length,
            }
            assert_allowlisted(entry, _WARNING_KEYS)
            warnings_out.append(entry)

    # Viewer warnings (from record.viewer_warnings)
    for vw in (record.viewer_warnings or []):
        code = vw.get("code")
        text_str = vw.get("text", "")
        v_entry: dict[str, Any] = {
            "source": "viewer",
            "code": code,
            "text": text_str,
            "text_truncated": False,
            "text_original_length": len(text_str) if isinstance(text_str, str) else None,
        }
        assert_allowlisted(v_entry, _WARNING_KEYS)
        warnings_out.append(v_entry)

    return warnings_out


# ---------------------------------------------------------------------------
# to_detail
# ---------------------------------------------------------------------------

def to_detail(record: "RunRecord") -> dict[str, Any]:
    """Serialise a :class:`RunRecord` to the nine-block detail dict."""
    obj: dict[str, Any] = {
        "run": _ser_run_block(record),
        "docker_state": _ser_docker_state_block(record),
        "timing": _ser_timing_block(record),
        "termination_markers": _ser_termination_markers_block(record),
        "evidence_summary": _ser_evidence_summary_block(record),
        "chart": _ser_chart_block(record),
        "runner_text": _ser_runner_text_block(record),
        "artifact_status": _ser_artifact_status_block(record),
        "warnings": _ser_warnings_block(record),
    }
    assert_allowlisted(obj, DETAIL_TOP_LEVEL_KEYS)
    return obj


# ---------------------------------------------------------------------------
# to_listing
# ---------------------------------------------------------------------------

def to_listing(listing: "dict[str, Any]") -> dict[str, Any]:
    """Serialise a listing payload (from loader.load_listing) to a dict."""
    facts_list = listing.get("facts", [])
    runs_items = [to_list_item(f) for f in facts_list]
    obj: dict[str, Any] = {
        "runs_dir_label": str(listing.get("runs_dir_label", "")),
        "total_found": int(listing.get("total_found", 0)),
        "listed": int(listing.get("listed", 0)),
        "truncated": bool(listing.get("truncated", False)),
        "max_listed": int(listing.get("max_listed", MAX_LISTED_RUNS_VALUE)),
        "runs": runs_items,
    }
    assert_allowlisted(obj, _LIST_ENVELOPE_KEYS)
    return obj


# Constant for use in to_listing without circular import
MAX_LISTED_RUNS_VALUE = 500
