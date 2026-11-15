"""Evidence record schema and validation (Requirements 2.9, 4.5, 4.7, 4.9, 5.5, 6.8, 7.6, 7.7, 12.2).

Pure functions over already-parsed JSON values. The issue list is deterministic: it follows the
schema's field order, then `null_reasons` problems and unknown fields in sorted key order, so it
does not depend on the insertion order of the input's keys. `validate_record` checks the envelope and then
the payload of every record type: the Sample and, since task 2.19, the payloads of `run_metadata`, `gap`,
`sequence_anomaly`, `rejection`, `repair` and `termination` (`validate_<type>`). Standard library plus
`coroner.common` only. Nothing here reads files, opens sockets, or serializes: `serialize_record` belongs to
the writer. Every validator returns ALL problems found, as `ValidationIssue` objects, never
raises on bad input, and never mutates its argument.

Reason codes (the Collector stores them in `rejection` records):
  invalid_run_id       any problem with a Run_Identifier
  missing_field        a required field is absent
  wrong_type           wrong JSON type (a boolean is never an integer, a float is never an integer)
  out_of_range         a value outside its allowed range or set
  invalid_format       a string with the wrong syntax (timestamp, container id)
  null_without_reason  a nullable field is null with no reason of 1 to 200 characters
  unknown_field        a field that the schema does not define
`invalid_json` and `body_too_large` are raised by the Collector before any validation here.
"""
from __future__ import annotations

import base64
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from coroner.common.timeutil import parse_utc_ms

SCHEMA_VERSION = 1

# The approved pattern. `$` also matches before a trailing newline, so always use
# `RUN_ID_RE.fullmatch(...)` (validate_run_id does), never `.match(...)`.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

RECORD_TYPES = (
    "run_metadata",
    "sample",
    "gap",
    "sequence_anomaly",
    "rejection",
    "repair",
    "termination",
)

# A null run_id is allowed in these record types at the schema level (design D27). The file-context
# rule (a null-run_id `repair` belongs only in `_rejections.jsonl`) is enforced by the reader and writer.
NULL_RUN_ID_TYPES = ("rejection", "repair")

INVALID_RUN_ID = "invalid_run_id"
MISSING_FIELD = "missing_field"
WRONG_TYPE = "wrong_type"
OUT_OF_RANGE = "out_of_range"
INVALID_FORMAT = "invalid_format"
NULL_WITHOUT_REASON = "null_without_reason"
UNKNOWN_FIELD = "unknown_field"
REASON_CODES = (
    INVALID_RUN_ID,
    MISSING_FIELD,
    WRONG_TYPE,
    OUT_OF_RANGE,
    INVALID_FORMAT,
    NULL_WITHOUT_REASON,
    UNKNOWN_FIELD,
)

# Sample (as sent by the Probe). `null_reasons` is the only optional field.
SAMPLE_REQUIRED_FIELDS = (
    "run_id",
    "seq",
    "ts",
    "rss_bytes",
    "request_count",
    "loop_delay_ms",
    "pid",
    "container_id",
    "container_id_source",
    "interval_ms",
    "dropped",
)
SAMPLE_OPTIONAL_FIELDS = ("null_reasons",)
NULLABLE_SAMPLE_FIELDS = ("rss_bytes", "request_count", "loop_delay_ms", "container_id")
CONTAINER_ID_SOURCES = ("mountinfo", "cgroup", "hostname", "none")
INTERVAL_MS_MIN, INTERVAL_MS_MAX = 100, 60000
DROPPED_MAX = 4_294_967_295
REASON_MIN_LEN, REASON_MAX_LEN = 1, 200

_CONTAINER_ID_RE = re.compile(r"(?:[0-9a-f]{64}|[0-9a-f]{12})")
_ENVELOPE_FIELDS = ("record_type", "schema_version", "run_id", "recv_time")
_SAMPLE_RECORD_FIELDS = frozenset(_ENVELOPE_FIELDS) | {"sample"}


@dataclass(frozen=True)
class ValidationIssue:
    """One problem with a record. `field` is a dotted path, `$` for the whole value."""

    field: str
    reason_code: str
    reason: str


def _brief(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _issue(field: str, code: str, reason: str) -> ValidationIssue:
    return ValidationIssue(field, code, reason)


def _sorted_keys(mapping: dict[Any, Any]) -> list[Any]:
    """Keys in an order that does not depend on insertion order.

    String keys sort by code point, so reports are identical for any permutation of the same
    object. Non-string keys cannot come from JSON, but a Python caller may pass them; they
    sort after the strings by `repr` and never make validation raise.
    """
    return sorted(mapping, key=lambda key: (0, key) if isinstance(key, str) else (1, repr(key)))


def _key_text(key: object) -> str:
    return key if isinstance(key, str) else repr(key)


def validate_run_id(value: object) -> ValidationIssue | None:
    """None when `value` is a valid Run_Identifier, else one issue with code invalid_run_id."""
    if not isinstance(value, str):
        return _issue("run_id", INVALID_RUN_ID, f"run_id must be a string, got {_brief(value)}")
    if value == "":
        return _issue("run_id", INVALID_RUN_ID, "run_id is empty")
    if len(value) > 64:
        return _issue("run_id", INVALID_RUN_ID, f"run_id longer than 64 characters ({len(value)})")
    if RUN_ID_RE.fullmatch(value) is None:
        return _issue(
            "run_id", INVALID_RUN_ID, f"run_id {_brief(value)} does not match {RUN_ID_RE.pattern}"
        )
    return None


def _validate_timestamp(field: str, value: object) -> ValidationIssue | None:
    if not isinstance(value, str):
        return _issue(field, WRONG_TYPE, f"{field} must be a string, got {_brief(value)}")
    try:
        parse_utc_ms(value)
    except ValueError:
        return _issue(field, INVALID_FORMAT, f"{field} {_brief(value)} is not YYYY-MM-DDTHH:mm:ss.sssZ")
    return None


def _validate_int(field: str, value: object, minimum: int, maximum: int | None = None) -> ValidationIssue | None:
    if not _is_int(value):
        return _issue(field, WRONG_TYPE, f"{field} must be an integer, got {_brief(value)}")
    assert isinstance(value, int)
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"at least {minimum}" if maximum is None else f"from {minimum} to {maximum}"
        return _issue(field, OUT_OF_RANGE, f"{field} {value} is not {bound}")
    return None


def _validate_loop_delay(value: object) -> ValidationIssue | None:
    if not _is_number(value):
        return _issue("loop_delay_ms", WRONG_TYPE, f"loop_delay_ms must be a number, got {_brief(value)}")
    if isinstance(value, float) and not math.isfinite(value):
        return _issue("loop_delay_ms", OUT_OF_RANGE, f"loop_delay_ms {value} is not finite")
    if value < 0:  # type: ignore[operator]
        return _issue("loop_delay_ms", OUT_OF_RANGE, f"loop_delay_ms {value} is negative")
    return None


def _validate_container_id(value: object) -> ValidationIssue | None:
    if not isinstance(value, str):
        return _issue("container_id", WRONG_TYPE, f"container_id must be a string or null, got {_brief(value)}")
    if _CONTAINER_ID_RE.fullmatch(value) is None:
        return _issue(
            "container_id", INVALID_FORMAT, f"container_id {_brief(value)} is not 64 or 12 lower-case hex characters"
        )
    return None


def _validate_container_id_source(value: object) -> ValidationIssue | None:
    if not isinstance(value, str):
        return _issue("container_id_source", WRONG_TYPE, f"container_id_source must be a string, got {_brief(value)}")
    if value not in CONTAINER_ID_SOURCES:
        return _issue(
            "container_id_source",
            OUT_OF_RANGE,
            f"container_id_source {_brief(value)} is not one of {', '.join(CONTAINER_ID_SOURCES)}",
        )
    return None


def _validate_value(field: str, value: object) -> ValidationIssue | None:
    """Validate one non-null Sample field value."""
    if field == "run_id":
        return validate_run_id(value)
    if field == "seq":
        return _validate_int(field, value, 0)
    if field == "ts":
        return _validate_timestamp(field, value)
    if field in ("rss_bytes", "request_count"):
        return _validate_int(field, value, 0)
    if field == "loop_delay_ms":
        return _validate_loop_delay(value)
    if field == "pid":  # the design gives no range for a process identifier, only the type
        return None if _is_int(value) else _issue(field, WRONG_TYPE, f"pid must be an integer, got {_brief(value)}")
    if field == "container_id":
        return _validate_container_id(value)
    if field == "container_id_source":
        return _validate_container_id_source(value)
    if field == "interval_ms":
        return _validate_int(field, value, INTERVAL_MS_MIN, INTERVAL_MS_MAX)
    if field == "dropped":
        return _validate_int(field, value, 0, DROPPED_MAX)
    raise AssertionError(f"no validator for {field}")


def _validate_null_reasons(sample: dict[str, Any]) -> tuple[list[ValidationIssue], dict[str, Any]]:
    """Validate the optional `null_reasons` object; return the issues and the usable reasons."""
    if "null_reasons" not in sample:
        return [], {}
    reasons = sample["null_reasons"]
    if not isinstance(reasons, dict):
        return [_issue("null_reasons", WRONG_TYPE, f"null_reasons must be an object, got {_brief(reasons)}")], {}
    issues: list[ValidationIssue] = []
    usable: dict[str, Any] = {}
    for key in _sorted_keys(reasons):
        reason = reasons[key]
        path = f"null_reasons.{_key_text(key)}"
        if key not in NULLABLE_SAMPLE_FIELDS:
            issues.append(_issue(path, UNKNOWN_FIELD, f"{_brief(key)} is not a nullable Sample field"))
        elif not isinstance(reason, str):
            issues.append(_issue(path, WRONG_TYPE, f"{path} must be a string, got {_brief(reason)}"))
        elif not REASON_MIN_LEN <= len(reason) <= REASON_MAX_LEN:
            issues.append(
                _issue(path, OUT_OF_RANGE, f"{path} has {len(reason)} characters, expected {REASON_MIN_LEN} to {REASON_MAX_LEN}")
            )
        elif sample.get(key, None) is not None:
            issues.append(_issue(path, INVALID_FORMAT, f"{path} gives a reason, but {key} is not null"))
        else:
            usable[key] = reason
    return issues, usable


def validate_sample(obj: object) -> list[ValidationIssue]:
    """All problems with a Sample as sent by the Probe. An empty list means valid."""
    if not isinstance(obj, dict):
        return [_issue("$", WRONG_TYPE, f"Sample must be an object, got {_brief(obj)}")]
    issues: list[ValidationIssue] = []
    reason_issues, usable_reasons = _validate_null_reasons(obj)
    for field in SAMPLE_REQUIRED_FIELDS:
        if field not in obj:
            if field == "run_id":
                issues.append(_issue(field, INVALID_RUN_ID, "run_id is missing"))
            else:
                issues.append(_issue(field, MISSING_FIELD, f"required field {field} is missing"))
            continue
        value = obj[field]
        if value is None and field in NULLABLE_SAMPLE_FIELDS:
            if field not in usable_reasons and not any(i.field == f"null_reasons.{field}" for i in reason_issues):
                issues.append(
                    _issue(field, NULL_WITHOUT_REASON, f"{field} is null without a reason of 1 to 200 characters in null_reasons")
                )
            continue
        problem = _validate_value(field, value)
        if problem is not None:
            issues.append(problem)
    issues.extend(reason_issues)
    allowed = set(SAMPLE_REQUIRED_FIELDS) | set(SAMPLE_OPTIONAL_FIELDS)
    for key in _sorted_keys(obj):
        if key not in allowed:
            issues.append(_issue(_key_text(key), UNKNOWN_FIELD, f"unknown field {_brief(key)}"))
    return issues


# --- payload contracts of the six non-sample record types (task 2.19) ----------------------------------------
# Every constraint below comes from the design's "Data Models" section (examples and prose), "Collector
# persistence" (gap rules) or the repair implementation. Where the design gives no range for a field, only its
# type is checked. Each validator takes the WHOLE record and returns payload issues only (the envelope is
# checked by `validate_record`). Issue order is deterministic: required fields in the order below, then
# cross-field checks, then unknown top-level fields in sorted key order.

FILE_SUFFIX = ".jsonl"  # same values as writer.FILE_SUFFIX and writer.REJECTIONS_STEM (a test compares them)
REJECTIONS_STEM = "_rejections"
FRAGMENT_CAP = 1024 * 1024  # same value as repair.FRAGMENT_CAP (a test compares them)
BODY_PREVIEW_MAX = 1024

RUN_METADATA_EVENTS = ("created", "resumed")
DURABILITY_LEVELS = ("process", "crash")
GAP_TYPES = ("sequence_discontinuity", "silence_timeout", "probe_reported_drop")
RECONCILIATION_VALUES = ("reconciled", "partial_overlap", "none", "not_applicable")
RANGE_STATUSES = ("known", "uncertain", "unknown")
ANOMALIES = ("duplicate", "reset")
REJECTION_REASON_CODES = (
    "invalid_json",
    "invalid_run_id",
    "missing_field",
    "wrong_type",
    "out_of_range",
    "invalid_format",
    "null_without_reason",
    "unknown_field",
    "body_too_large",
)
REPAIR_REASONS = ("incomplete_last_line", "missing_trailing_newline", "invalid_json_last_line")
REPAIR_ACTIONS = ("appended_newline", "none_needed_already_terminated")
TERMINATION_SOURCE = "experiment_runner"
CAUSE_LABELS = ("controlled_fatal_process_exit", "sigkill_sent_by_runner", "docker_oom_kill", "unknown")
HTTP_STATUS_MIN, HTTP_STATUS_MAX = 400, 599  # D31: HTTP error statuses only (the design lists 413 and 422)

_Check = Callable[[str, object], list[ValidationIssue]]


def _one(problem: ValidationIssue | None) -> list[ValidationIssue]:
    return [] if problem is None else [problem]


def _c_str(path: str, value: object) -> list[ValidationIssue]:
    if not isinstance(value, str):
        return [_issue(path, WRONG_TYPE, f"{path} must be a string, got {_brief(value)}")]
    return []


def _c_nonempty_str(path: str, value: object) -> list[ValidationIssue]:
    if not isinstance(value, str):
        return [_issue(path, WRONG_TYPE, f"{path} must be a string, got {_brief(value)}")]
    if value == "":
        return [_issue(path, OUT_OF_RANGE, f"{path} is empty")]
    return []


def _c_bool(path: str, value: object) -> list[ValidationIssue]:
    if not isinstance(value, bool):
        return [_issue(path, WRONG_TYPE, f"{path} must be a boolean, got {_brief(value)}")]
    return []


def _c_ts(path: str, value: object) -> list[ValidationIssue]:
    return _one(_validate_timestamp(path, value))


def _c_int(minimum: int, maximum: int | None = None) -> _Check:
    return lambda path, value: _one(_validate_int(path, value, minimum, maximum))


def _c_enum(allowed: tuple[str, ...]) -> _Check:
    def check(path: str, value: object) -> list[ValidationIssue]:
        if not isinstance(value, str):
            return [_issue(path, WRONG_TYPE, f"{path} must be a string, got {_brief(value)}")]
        if value not in allowed:
            return [_issue(path, OUT_OF_RANGE, f"{path} {_brief(value)} is not one of {', '.join(allowed)}")]
        return []

    return check


def _evidence_file_name_problem(value: str, *, allow_rejections: bool) -> str | None:
    if not value.endswith(FILE_SUFFIX):
        return f"does not end with {FILE_SUFFIX}"
    stem = value[: -len(FILE_SUFFIX)]
    if stem == REJECTIONS_STEM:
        return None if allow_rejections else f"{REJECTIONS_STEM}{FILE_SUFFIX} is not a run file"
    problem = validate_run_id(stem)
    return None if problem is None else f"stem is not a valid run_id ({problem.reason})"


def _c_file_name(allow_rejections: bool) -> _Check:
    def check(path: str, value: object) -> list[ValidationIssue]:
        if not isinstance(value, str):
            return [_issue(path, WRONG_TYPE, f"{path} must be a string, got {_brief(value)}")]
        problem = _evidence_file_name_problem(value, allow_rejections=allow_rejections)
        if problem is not None:
            return [_issue(path, INVALID_FORMAT, f"{path} {_brief(value)} is not an evidence file name: {problem}")]
        return []

    return check


def _file_identity_issue(obj: dict[Any, Any], field: str, *, null_run_file: str | None, allow_rejections: bool) -> list[ValidationIssue]:
    """D31 (8): the named file is `<run_id>.jsonl` for a valid run_id, or `null_run_file` for a null run_id.

    Skipped when the name is not a string or has the wrong form (reported separately) or when the run_id is
    missing or invalid (reported by the envelope check).
    """
    name, run_id = obj.get(field), obj.get("run_id")
    if not isinstance(name, str) or _evidence_file_name_problem(name, allow_rejections=allow_rejections) is not None:
        return []
    if run_id is None and "run_id" in obj and null_run_file is not None:
        expected = null_run_file
    elif isinstance(run_id, str) and validate_run_id(run_id) is None:
        expected = run_id + FILE_SUFFIX
    else:
        return []
    if name != expected:
        return [_issue(field, OUT_OF_RANGE, f"{field} {_brief(name)} is not the file of run_id {_brief(run_id)}, expected {expected!r}")]
    return []


def _decode_strict_b64(value: str) -> bytes | None:
    """The bytes of canonical base64 text, or None: no whitespace, correct padding, nothing non-canonical."""
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:  # binascii.Error is a ValueError; a non-ASCII str raises ValueError too
        return None
    return raw if base64.b64encode(raw).decode("ascii") == value else None


def _c_b64(path: str, value: object) -> list[ValidationIssue]:
    if not isinstance(value, str):
        return [_issue(path, WRONG_TYPE, f"{path} must be a string, got {_brief(value)}")]
    if _decode_strict_b64(value) is None:
        return [_issue(path, INVALID_FORMAT, f"{path} is not canonical base64 (alphabet, padding, no whitespace)")]
    return []


def _c_gap_id_list(path: str, value: object) -> list[ValidationIssue]:
    if not isinstance(value, list):
        return [_issue(path, WRONG_TYPE, f"{path} must be a list of gap ids, got {_brief(value)}")]
    out: list[ValidationIssue] = []
    for index, item in enumerate(value):
        out.extend(_c_nonempty_str(f"{path}[{index}]", item))
    return out


def _c_unknown_marker(path: str, value: object) -> list[ValidationIssue]:
    if not isinstance(value, str):
        return [_issue(path, WRONG_TYPE, f"{path} must be the string 'unknown', got {_brief(value)}")]
    if value != "unknown":
        return [_issue(path, OUT_OF_RANGE, f"{path} {_brief(value)} is not 'unknown'")]
    return []


def _check_fields(
    obj: dict[Any, Any],
    fields: Sequence[tuple[str, _Check]],
    optional: Sequence[tuple[str, _Check]] = (),
    prefix: str = "",
    ignore: tuple[str, ...] = (),
) -> list[ValidationIssue]:
    """Required fields in order, then optional ones, then unknown keys in sorted order."""
    issues: list[ValidationIssue] = []
    for name, check in fields:
        path = prefix + name
        if name not in obj:
            issues.append(_issue(path, MISSING_FIELD, f"required field {path} is missing"))
        else:
            issues.extend(check(path, obj[name]))
    for name, check in optional:
        if name in obj:
            issues.extend(check(prefix + name, obj[name]))
    known = {n for n, _ in fields} | {n for n, _ in optional} | set(ignore)
    for key in _sorted_keys(obj):
        if key not in known:
            issues.append(_issue(prefix + _key_text(key), UNKNOWN_FIELD, f"unknown field {_brief(key)}"))
    return issues


def validate_run_metadata(obj: dict[Any, Any]) -> list[ValidationIssue]:
    """Payload of a `run_metadata` record. `crash` durability means fsync before the acknowledgement."""
    issues = _check_fields(
        obj,
        [
            ("event", _c_enum(RUN_METADATA_EVENTS)),
            ("durability", _c_enum(DURABILITY_LEVELS)),
            ("fsync_before_ack", _c_bool),
            ("evidence_file", _c_file_name(False)),
            ("collector_version", _c_str),
        ],
        ignore=_ENVELOPE_FIELDS,
    )
    durability, fsync = obj.get("durability"), obj.get("fsync_before_ack")
    if durability in DURABILITY_LEVELS and isinstance(fsync, bool) and isinstance(durability, str):
        if (durability == "crash") != fsync:
            issues.append(
                _issue("fsync_before_ack", OUT_OF_RANGE, f"fsync_before_ack {fsync} contradicts durability {durability!r} (crash requires true, process requires false)")
            )
    issues.extend(_file_identity_issue(obj, "evidence_file", null_run_file=None, allow_rejections=False))
    return issues


_GAP_COMMON: list[tuple[str, _Check]] = [
    ("gap_id", _c_nonempty_str),
    ("gap_type", _c_enum(GAP_TYPES)),
    ("reconciliation", _c_enum(RECONCILIATION_VALUES)),
    ("reconciles_with", _c_gap_id_list),
]
_GAP_BY_TYPE: dict[str, list[tuple[str, _Check]]] = {
    "sequence_discontinuity": [
        ("start_time", _c_ts),
        ("end_time", _c_ts),
        ("first_missing_seq", _c_int(0)),
        ("last_missing_seq", _c_int(0)),
        ("missing_count", _c_int(1)),
        ("detected_at_seq", _c_int(0)),
    ],
    "probe_reported_drop": [
        ("start_time", _c_ts),
        ("end_time", _c_ts),
        ("dropped_count", _c_int(1, DROPPED_MAX)),
        ("range_status", _c_enum(RANGE_STATUSES)),
        ("detected_at_seq", _c_int(0)),
    ],
    "silence_timeout": [
        ("start_time", _c_ts),
        ("end_time", _c_ts),
        ("last_sample_time", _c_ts),
        ("detected_time", _c_ts),
        ("threshold_ms", _c_int(0)),
        ("interval_ms", _c_int(INTERVAL_MS_MIN, INTERVAL_MS_MAX)),
        ("sample_count", _c_unknown_marker),
    ],
}


def _validate_drop_range(obj: dict[Any, Any]) -> list[ValidationIssue]:
    """first_missing_seq and last_missing_seq of a probe_reported_drop: integers only when range_status is known."""
    issues: list[ValidationIssue] = []
    status = obj.get("range_status")
    names = ("first_missing_seq", "last_missing_seq")
    for name in names:
        if name not in obj:
            issues.append(_issue(name, MISSING_FIELD, f"required field {name} is missing (null unless range_status is known)"))
        elif status == "known":
            issues.extend(_one(_validate_int(name, obj[name], 0)))
        elif status in RANGE_STATUSES and obj[name] is not None:
            issues.append(_issue(name, OUT_OF_RANGE, f"{name} must be null when range_status is {status!r}, got {_brief(obj[name])}"))
    first, last = obj.get("first_missing_seq"), obj.get("last_missing_seq")
    if status == "known" and _is_int(first) and _is_int(last) and first > last:  # type: ignore[operator]
        issues.append(_issue("last_missing_seq", OUT_OF_RANGE, f"last_missing_seq {last} is less than first_missing_seq {first}"))
    elif status == "known" and _is_int(first) and _is_int(last) and first >= 0 and last >= 0:  # type: ignore[operator]
        # D31 (5): the range is derived as [detected_at_seq - dropped_count, detected_at_seq - 1] and starts at 0 or later.
        dropped, detected = obj.get("dropped_count"), obj.get("detected_at_seq")
        if _is_int(dropped) and _is_int(detected) and 1 <= dropped <= DROPPED_MAX and detected >= 0:  # type: ignore[operator]
            want_first, want_last = detected - dropped, detected - 1  # type: ignore[operator]
            if first != want_first:
                issues.append(_issue("first_missing_seq", OUT_OF_RANGE, f"first_missing_seq {first} is not detected_at_seq - dropped_count = {want_first}"))
            if last != want_last:
                issues.append(_issue("last_missing_seq", OUT_OF_RANGE, f"last_missing_seq {last} is not detected_at_seq - 1 = {want_last}"))
    return issues


def validate_gap(obj: dict[Any, Any]) -> list[ValidationIssue]:
    """Payload of a `gap` record. The fields depend on `gap_type`; an unusable `gap_type` is the only issue then
    reported for the type-specific part. `reconciliation` and `reconciles_with` are advisory and only
    type-checked (the reader recomputes them from the ranges)."""
    gap_type = obj.get("gap_type")
    specific = _GAP_BY_TYPE.get(gap_type) if isinstance(gap_type, str) else None
    issues = _check_fields(
        obj,
        _GAP_COMMON[:2] + (specific or []) + _GAP_COMMON[2:],
        optional=[("range_note", _c_str)] if gap_type == "probe_reported_drop" else [],
        ignore=_ENVELOPE_FIELDS
        + (("first_missing_seq", "last_missing_seq") if gap_type == "probe_reported_drop" else ())
        + (tuple(k for k in obj if isinstance(k, str)) if specific is None else ()),  # unusable gap_type: no unknown-field noise
    )
    if gap_type == "sequence_discontinuity":
        first, last, count = obj.get("first_missing_seq"), obj.get("last_missing_seq"), obj.get("missing_count")
        if _is_int(first) and _is_int(last) and first >= 0 and last >= 0:  # type: ignore[operator]
            if first > last:  # type: ignore[operator]
                issues.append(_issue("last_missing_seq", OUT_OF_RANGE, f"last_missing_seq {last} is less than first_missing_seq {first}"))
            elif _is_int(count) and count != last - first + 1:  # type: ignore[operator]
                issues.append(_issue("missing_count", OUT_OF_RANGE, f"missing_count {count} is not last_missing_seq - first_missing_seq + 1 = {last - first + 1}"))  # type: ignore[operator]
    elif gap_type == "probe_reported_drop":
        issues.extend(_validate_drop_range(obj))
    return issues


def validate_sequence_anomaly(obj: dict[Any, Any]) -> list[ValidationIssue]:
    """Payload of a `sequence_anomaly` record; the nested `sample` is a full Sample that agrees with `run_id` and `seq`."""
    issues = _check_fields(
        obj,
        [
            ("anomaly", _c_enum(ANOMALIES)),
            ("seq", _c_int(0)),
            ("previous_seq", _c_int(0)),
            ("sample", lambda path, value: []),  # checked below with dotted paths
        ],
        ignore=_ENVELOPE_FIELDS,
    )
    if "sample" in obj:
        for nested in validate_sample(obj["sample"]):
            field = "sample" if nested.field == "$" else f"sample.{nested.field}"
            issues.append(_issue(field, nested.reason_code, nested.reason))
        sample = obj["sample"] if isinstance(obj["sample"], dict) else {}
        run_id, nested_run_id = obj.get("run_id"), sample.get("run_id")
        if isinstance(run_id, str) and isinstance(nested_run_id, str) and run_id != nested_run_id:
            issues.append(_issue("run_id", INVALID_RUN_ID, f"run_id {_brief(run_id)} differs from sample.run_id {_brief(nested_run_id)}"))
        seq, nested_seq = obj.get("seq"), sample.get("seq")
        if _is_int(seq) and _is_int(nested_seq) and seq != nested_seq:
            issues.append(_issue("seq", OUT_OF_RANGE, f"seq {seq} differs from sample.seq {nested_seq}"))
    # D31 (6): a duplicate is at or below the highest accepted number, a reset strictly below it.
    anomaly, seq, previous = obj.get("anomaly"), obj.get("seq"), obj.get("previous_seq")
    if _is_int(seq) and _is_int(previous) and seq >= 0 and previous >= 0 and anomaly in ANOMALIES:  # type: ignore[operator]
        if anomaly == "duplicate" and seq > previous:  # type: ignore[operator]
            issues.append(_issue("seq", OUT_OF_RANGE, f"duplicate seq {seq} is above previous_seq {previous}"))
        elif anomaly == "reset" and seq >= previous:  # type: ignore[operator]
            issues.append(_issue("seq", OUT_OF_RANGE, f"reset seq {seq} is not below previous_seq {previous}"))
    return issues


def validate_rejection(obj: dict[Any, Any]) -> list[ValidationIssue]:
    """Payload of a `rejection` record. The preview is at most the first 1,024 bytes of the body, as base64."""
    issues = _check_fields(
        obj,
        [
            ("http_status", _c_int(HTTP_STATUS_MIN, HTTP_STATUS_MAX)),
            ("reason_code", _c_enum(REJECTION_REASON_CODES)),
            ("field", _c_str),
            ("reason", _c_nonempty_str),
            ("body_len", _c_int(0)),
            ("body_preview_b64", _c_b64),
        ],
        ignore=_ENVELOPE_FIELDS,
    )
    preview = obj.get("body_preview_b64")
    raw = _decode_strict_b64(preview) if isinstance(preview, str) else None
    if raw is not None and len(raw) > BODY_PREVIEW_MAX:
        issues.append(_issue("body_preview_b64", OUT_OF_RANGE, f"body_preview_b64 holds {len(raw)} bytes, at most {BODY_PREVIEW_MAX} allowed"))
    body_len = obj.get("body_len")
    if raw is not None and _is_int(body_len) and body_len >= 0 and body_len < len(raw):  # type: ignore[operator]
        issues.append(_issue("body_len", OUT_OF_RANGE, f"body_len {body_len} is less than the {len(raw)} bytes of body_preview_b64"))
    return issues


def validate_repair(obj: dict[Any, Any]) -> list[ValidationIssue]:
    """Payload of a `repair` record (D28, D30). `fragment_len` is the true length of the line the repair found,
    `fragment_b64` its first FRAGMENT_CAP bytes, without the repair's own invalidating marker."""
    issues = _check_fields(
        obj,
        [
            ("file", _c_file_name(True)),
            ("line_no", _c_int(1)),
            ("fragment_len", _c_int(0)),
            ("fragment_b64", _c_b64),
            ("fragment_truncated", _c_bool),
            ("reason", _c_enum(REPAIR_REASONS)),
            ("action", _c_enum(REPAIR_ACTIONS)),
        ],
        ignore=_ENVELOPE_FIELDS,
    )
    length, encoded, truncated = obj.get("fragment_len"), obj.get("fragment_b64"), obj.get("fragment_truncated")
    if _is_int(length) and length >= 0:  # type: ignore[operator]
        raw = _decode_strict_b64(encoded) if isinstance(encoded, str) else None
        if raw is not None and len(raw) != min(length, FRAGMENT_CAP):  # type: ignore[type-var]
            issues.append(
                _issue("fragment_b64", OUT_OF_RANGE, f"fragment_b64 holds {len(raw)} bytes, expected min(fragment_len, {FRAGMENT_CAP}) = {min(length, FRAGMENT_CAP)}")  # type: ignore[type-var]
            )
        if isinstance(truncated, bool) and truncated != (length > FRAGMENT_CAP):  # type: ignore[operator]
            issues.append(_issue("fragment_truncated", OUT_OF_RANGE, f"fragment_truncated {truncated} contradicts fragment_len {length} (cap {FRAGMENT_CAP})"))
    reason, action = obj.get("reason"), obj.get("action")
    if reason in REPAIR_REASONS and action in REPAIR_ACTIONS:
        terminated = action == "none_needed_already_terminated"
        if terminated != (reason == "invalid_json_last_line"):
            issues.append(_issue("action", OUT_OF_RANGE, f"action {action!r} does not fit reason {reason!r}"))
    issues.extend(_file_identity_issue(obj, "file", null_run_file=REJECTIONS_STEM + FILE_SUFFIX, allow_rejections=True))
    return issues


_CONTAINER_STATE_FIELDS: list[tuple[str, _Check]] = [
    ("status", _c_str),
    ("exit_code", lambda path, value: [] if _is_int(value) else [_issue(path, WRONG_TYPE, f"{path} must be an integer, got {_brief(value)}")]),
    ("oom_killed", _c_bool),
]


def _c_container_state(path: str, value: object) -> list[ValidationIssue]:
    if not isinstance(value, dict):
        return [_issue(path, WRONG_TYPE, f"{path} must be an object, got {_brief(value)}")]
    return _check_fields(value, _CONTAINER_STATE_FIELDS, prefix=f"{path}.")


def validate_termination(obj: dict[Any, Any]) -> list[ValidationIssue]:
    """Payload of a `termination` record. Only the shape is checked: the cause is derived by the runner, never here."""
    return _check_fields(
        obj,
        [
            ("source", _c_enum((TERMINATION_SOURCE,))),
            ("observed_time", _c_ts),
            ("container_state", _c_container_state),
            ("cause_label", _c_enum(CAUSE_LABELS)),
            ("note", _c_str),
        ],
        ignore=_ENVELOPE_FIELDS,
    )


_PAYLOAD_VALIDATORS: dict[str, Callable[[dict[Any, Any]], list[ValidationIssue]]] = {
    "run_metadata": validate_run_metadata,
    "gap": validate_gap,
    "sequence_anomaly": validate_sequence_anomaly,
    "rejection": validate_rejection,
    "repair": validate_repair,
    "termination": validate_termination,
}


def validate_record(obj: object) -> list[ValidationIssue]:
    """All problems with a stored record: the envelope, then the payload of its record type.

    Checked for every record type: `record_type` in RECORD_TYPES, `schema_version` equal to
    SCHEMA_VERSION, `run_id` a valid Run_Identifier (null only for `rejection` and `repair`; which
    file may hold a null run_id is decided by the reader and the writer, design D27), and a
    `recv_time` timestamp. For `sample` records the nested `sample` must be a valid Sample
    whose `run_id` equals the envelope's, and no other top-level field is allowed. The payload of
    `run_metadata`, `gap`, `sequence_anomaly`, `rejection`, `repair` and `termination` records is
    validated by `validate_<type>` (task 2.19): required fields, types, allowed values and ranges,
    cross-field rules, and no unknown top-level fields. Which file may hold a record, and whether a
    `repair` or `run_metadata` file may sit in, is not checked here (reader, D27). The named file must match
    the `run_id` (D31).
    """
    if not isinstance(obj, dict):
        return [_issue("$", WRONG_TYPE, f"record must be an object, got {_brief(obj)}")]
    issues: list[ValidationIssue] = []
    record_type = obj.get("record_type")
    if "record_type" not in obj:
        issues.append(_issue("record_type", MISSING_FIELD, "required field record_type is missing"))
    elif not isinstance(record_type, str):
        issues.append(_issue("record_type", WRONG_TYPE, f"record_type must be a string, got {_brief(record_type)}"))
    elif record_type not in RECORD_TYPES:
        issues.append(_issue("record_type", OUT_OF_RANGE, f"record_type {_brief(record_type)} is not one of {', '.join(RECORD_TYPES)}"))

    if "schema_version" not in obj:
        issues.append(_issue("schema_version", MISSING_FIELD, "required field schema_version is missing"))
    elif not _is_int(obj["schema_version"]):
        issues.append(_issue("schema_version", WRONG_TYPE, f"schema_version must be an integer, got {_brief(obj['schema_version'])}"))
    elif obj["schema_version"] != SCHEMA_VERSION:
        issues.append(_issue("schema_version", OUT_OF_RANGE, f"schema_version {obj['schema_version']} is not {SCHEMA_VERSION}"))

    run_id = obj.get("run_id")
    if "run_id" not in obj:
        issues.append(_issue("run_id", INVALID_RUN_ID, "run_id is missing"))
    elif run_id is None:
        if record_type not in NULL_RUN_ID_TYPES:
            issues.append(_issue("run_id", INVALID_RUN_ID, "run_id may be null only in rejection and repair records"))
    else:
        problem = validate_run_id(run_id)
        if problem is not None:
            issues.append(problem)

    if "recv_time" not in obj:
        issues.append(_issue("recv_time", MISSING_FIELD, "required field recv_time is missing"))
    else:
        problem = _validate_timestamp("recv_time", obj["recv_time"])
        if problem is not None:
            issues.append(problem)

    if record_type == "sample":
        if "sample" not in obj:
            issues.append(_issue("sample", MISSING_FIELD, "required field sample is missing"))
        else:
            for nested in validate_sample(obj["sample"]):
                field = "sample" if nested.field == "$" else f"sample.{nested.field}"
                issues.append(_issue(field, nested.reason_code, nested.reason))
            nested_run_id = obj["sample"].get("run_id") if isinstance(obj["sample"], dict) else None
            if isinstance(run_id, str) and isinstance(nested_run_id, str) and run_id != nested_run_id:
                issues.append(_issue("run_id", INVALID_RUN_ID, f"run_id {_brief(run_id)} differs from sample.run_id {_brief(nested_run_id)}"))
        for key in _sorted_keys(obj):
            if key not in _SAMPLE_RECORD_FIELDS:
                issues.append(_issue(_key_text(key), UNKNOWN_FIELD, f"unknown field {_brief(key)} in a sample record"))
    elif isinstance(record_type, str) and record_type in _PAYLOAD_VALIDATORS:
        issues.extend(_PAYLOAD_VALIDATORS[record_type](obj))
    return issues