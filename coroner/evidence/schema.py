"""Evidence record schema and validation (Requirements 2.9, 4.5, 4.7, 4.9, 7.6, 7.7).

Pure functions over already-parsed JSON values. The issue list is deterministic: it follows the
schema's field order, then `null_reasons` problems and unknown fields in sorted key order, so it
does not depend on the insertion order of the input's keys. Standard library plus `coroner.common`
only. Nothing here reads files, opens sockets, or serializes: `serialize_record` belongs to
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

import math
import re
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


def validate_record(obj: object) -> list[ValidationIssue]:
    """All problems with a stored record's envelope, and with the nested Sample of a `sample` record.

    Checked for every record type: `record_type` in RECORD_TYPES, `schema_version` equal to
    SCHEMA_VERSION, `run_id` a valid Run_Identifier (null only for `rejection`), and a
    `recv_time` timestamp. For `sample` records the nested `sample` must be a valid Sample
    whose `run_id` equals the envelope's, and no other top-level field is allowed. The
    type-specific payload of the other record types is not validated here.
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
        if record_type != "rejection":
            issues.append(_issue("run_id", INVALID_RUN_ID, "run_id may be null only in rejection records"))
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
    return issues