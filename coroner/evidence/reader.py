"""Evidence reader: read a JSONL evidence file, quarantine bad lines, never modify anything.

Requirements 5.9, 5.10, 5.11, 6.5 to 6.7, 6.11 to 6.13, 12.5; design D12, D19, D26, D27, D28. Standard library plus
`coroner.evidence.schema`, `coroner.evidence.gaps`, `coroner.evidence.writer` (for the `_rejections` name) and
`coroner.common`. `summarize` and `RunSummary` (task 2.12) are at the end of this module; their rules are in
the `summarize` docstring.

What `read_evidence` does
* Reads bytes (a `Path` is opened read-only in binary mode; `bytes` are used as given), splits on the byte
  `\\n`, decodes each line as strict UTF-8, parses it with `json.loads` (`NaN` and `Infinity` are refused, as
  in `repair.py`), and validates it with `schema.validate_record`.
* A line that is not a valid record goes to `ReadResult.errors` as a `ReadError` and never to `records`.
  Reading continues. Line numbers are the 1-based numbers in the file. `byte_length` excludes the newline.
* A last line with no terminating newline is `incomplete_last_line`, even if its bytes parse (an
  acknowledged record always ends in a newline). A file that ends in a newline has no extra empty line.
* Repair linkage (D19). A `repair` record names a line by `line_no`; it always comes after the line it
  names, so linkage is two-pass. A line named by a repair record with `action: "appended_newline"` is
  quarantined as `isolated_by_repair`, even when it is a perfectly valid Sample, because it needed an
  isolating newline and so was never acknowledged. The underlying problem (if any) is in `detail`. A line
  named by any other repair action keeps its own kind, with `has_repair_record` true. A repair record only
  links lines that precede it, and, when the file name is known, only if its `file` field is not a
  different file name. A repair record that belongs in the file links even when it is itself quarantined
  (isolated or unterminated), because linking can only quarantine more, never less. A repair record that
  does not belong in the file (file context below) links nothing.
* File context (D27). The file name, from the Path or from `name`, decides which records may be there.
  `_rejections.jsonl` holds only `rejection` and `repair` records with a null `run_id`. `<run_id>.jsonl`
  holds only records whose `run_id` is that run (a null `run_id`, in any record type, is invalid there).
  When the name gives no context, a null `run_id` is accepted only for `rejection` records and no
  run-versus-file comparison is made. A violation is `invalid_record`.
* The payloads of the six non-sample record types are not validated (task 2.19), and `repair` fields other
  than `action`, `line_no` and `file` are not used.

Interrupted repairs (D27, narrowed by D30): since D30, `repair_file` writes a NUL marker right after an
unterminated fragment, before the isolating newline. The physical isolated line is the fragment plus one NUL
per repair attempt whose marker persisted, so `byte_length` is that physical length, and the line never
parses as JSON. If a repair was interrupted after the marker but before a complete repair record, the line
is quarantined as an ordinary invalid line (`invalid_json`, or `invalid_utf8` for a non-UTF-8 fragment),
with `has_repair_record` false unless a later `none_needed_already_terminated` record names it. The reader
cannot invent a missing audit record. A file in which only a bare newline follows a complete record (no
marker; for example a file repaired before D30) still reads that line as valid.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from coroner.evidence.gaps import missing_sample_count, reconcile, unreconciled_drops
from coroner.evidence.schema import validate_record, validate_run_id
from coroner.evidence.writer import FILE_SUFFIX, REJECTIONS_STEM

__all__ = ["ReadError", "ReadResult", "RunSummary", "read_evidence", "summarize"]

# ReadError kinds
INVALID_UTF8 = "invalid_utf8"
INVALID_JSON = "invalid_json"
NOT_AN_OBJECT = "not_an_object"
INCOMPLETE_LAST_LINE = "incomplete_last_line"
INVALID_RECORD = "invalid_record"
ISOLATED_BY_REPAIR = "isolated_by_repair"
READ_ERROR_KINDS = (INVALID_UTF8, INVALID_JSON, NOT_AN_OBJECT, INCOMPLETE_LAST_LINE, INVALID_RECORD, ISOLATED_BY_REPAIR)

_UNNAMED = "<bytes>"
_MAX_ISSUES_IN_DETAIL = 5


@dataclass(frozen=True)
class ReadError:
    """A Malformed_Line: reported and quarantined, never an evidence record."""

    file: str
    line_no: int
    byte_length: int  # length of the offending line in bytes, excluding the newline
    kind: str
    detail: str
    has_repair_record: bool


@dataclass
class ReadResult:
    path: str
    records: list[Mapping[str, Any]] = field(default_factory=list)  # valid records only, in file order
    line_numbers: list[int] = field(default_factory=list)  # parallel to `records`
    errors: list[ReadError] = field(default_factory=list)  # in line order


@dataclass(frozen=True)
class _Context:
    kind: str  # "run", "rejections" or "unknown"
    run_id: str | None = None


def _context_for(file_name: str) -> _Context:
    base = PurePath(file_name).name
    if not base.endswith(FILE_SUFFIX):
        return _Context("unknown")
    stem = base[: -len(FILE_SUFFIX)]
    if stem == REJECTIONS_STEM:
        return _Context("rejections")
    if validate_run_id(stem) is None:
        return _Context("run", stem)
    return _Context("unknown")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


def _context_problem(record: Mapping[str, Any], context: _Context) -> str | None:
    """Why a schema-valid record does not belong in this file, or None."""
    record_type, run_id = record.get("record_type"), record.get("run_id")
    if context.kind == "rejections":
        if record_type not in ("rejection", "repair") or run_id is not None:
            return f"{_describe(record_type, run_id)} does not belong in {REJECTIONS_STEM}{FILE_SUFFIX}: only rejection and repair records with a null run_id do"
    elif context.kind == "run":
        if run_id != context.run_id:
            return f"{_describe(record_type, run_id)} does not belong in the file of run {context.run_id!r}"
    elif run_id is None and record_type != "rejection":
        return f"{_describe(record_type, run_id)} has a null run_id, which only a rejection record may have outside {REJECTIONS_STEM}{FILE_SUFFIX}"
    return None


def _describe(record_type: object, run_id: object) -> str:
    return f"a {record_type} record with run_id {run_id!r}"


def _parse_line(raw: bytes) -> tuple[str, Any, str]:
    """Return (status, value, detail). status is "ok" or one of the error kinds that parsing can produce."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return INVALID_UTF8, None, f"not valid UTF-8 (byte offset {exc.start}: {exc.reason})"
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        return INVALID_JSON, None, f"not valid JSON ({type(exc).__name__}: {exc})"
    if not isinstance(value, dict):
        return NOT_AN_OBJECT, None, f"JSON value is a {type(value).__name__}, not an object"
    return "ok", value, ""


def _split_lines(data: bytes) -> tuple[list[bytes], bool]:
    """Lines (without newlines) and whether the last one has no terminating newline. An empty file has no lines."""
    if not data:
        return [], False
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
        return lines, False
    return lines, True


def _repair_links(parsed: list[tuple[str, Any, str]], base_name: str | None, context: _Context) -> dict[int, set[str]]:
    """Map line number -> set of repair actions that name it, from valid repair records that follow it.

    A repair record that does not belong in this file (wrong run, or a null run_id outside the rejections
    file) is foreign and links nothing. One that is merely quarantined for being unacknowledged still links.
    """
    links: dict[int, set[str]] = {}
    for index, (status, value, _) in enumerate(parsed):
        if status != "ok" or value.get("record_type") != "repair" or validate_record(value) or _context_problem(value, context):
            continue
        own_line = index + 1
        target, action, named_file = value.get("line_no"), value.get("action"), value.get("file")
        if not isinstance(target, int) or isinstance(target, bool) or not 1 <= target < own_line:
            continue
        if not isinstance(action, str):
            continue
        if base_name is not None and isinstance(named_file, str) and named_file != base_name:
            continue
        links.setdefault(target, set()).add(action)
    return links


def read_evidence(source: Path | bytes, *, name: str | None = None) -> ReadResult:
    """Read evidence from a file `Path` or from `bytes`. Never modifies anything.

    `name` is the file name used in errors and for the file-context rules. For a Path it defaults to the
    file name; for bytes it defaults to "<bytes>" (no context). A missing or unreadable file raises
    OSError as usual. Any other source type raises TypeError.
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        file_name = name if name is not None else _UNNAMED
        shown_path = file_name
    elif isinstance(source, Path):
        with open(source, "rb") as handle:  # read-only, binary: no newline translation
            data = handle.read()
        file_name = name if name is not None else source.name
        shown_path = str(source)
    else:
        raise TypeError(f"source must be a Path or bytes, got {type(source).__name__}")

    context = _context_for(file_name)
    base_name = PurePath(file_name).name if context.kind != "unknown" else None
    lines, last_unterminated = _split_lines(data)
    parsed = [_parse_line(raw) for raw in lines]
    links = _repair_links(parsed, base_name, context)

    result = ReadResult(path=shown_path)
    for index, raw in enumerate(lines):
        line_no = index + 1
        status, value, detail = parsed[index]
        actions = links.get(line_no, set())
        has_repair = bool(actions)
        kind: str | None = None
        if status != "ok":
            kind = status
        else:
            issues = validate_record(value)
            if issues:
                kind = INVALID_RECORD
                shown = "; ".join(f"{i.field}: {i.reason_code}" for i in issues[:_MAX_ISSUES_IN_DETAIL])
                detail = f"not a valid record ({shown}{'; ...' if len(issues) > _MAX_ISSUES_IN_DETAIL else ''})"
            else:
                problem = _context_problem(value, context)
                if problem is not None:
                    kind, detail = INVALID_RECORD, problem
        if last_unterminated and line_no == len(lines):
            detail = f"last line has no terminating newline ({kind or 'parses as a record'}{': ' + detail if detail else ''})"
            kind = INCOMPLETE_LAST_LINE
        elif "appended_newline" in actions:
            underlying = f" (it also is {kind}: {detail})" if kind is not None else ""
            detail = f"isolated by a repair record with action appended_newline, so never acknowledged{underlying}"
            kind = ISOLATED_BY_REPAIR
        if kind is None:
            result.records.append(value)
            result.line_numbers.append(line_no)
        else:
            result.errors.append(ReadError(file_name, line_no, len(raw), kind, detail, has_repair))
    return result


# --- summary (task 2.12; Requirements 6.5 to 6.7, 6.11 to 6.13, 12.5; design "Gap and sequence handling") ----------

NONE_RECEIVED = "none received"
UNKNOWN_CAUSE = "unknown"
UNOBSERVED_NOTE = "The interval between the last received Sample and the actual termination time is unobserved."
_SEQUENCE_DISCONTINUITY = "sequence_discontinuity"
_PROBE_REPORTED_DROP = "probe_reported_drop"
_SILENCE_TIMEOUT = "silence_timeout"
_GAP_TYPES = (_SEQUENCE_DISCONTINUITY, _SILENCE_TIMEOUT, _PROBE_REPORTED_DROP)


@dataclass
class RunSummary:
    run_id: str | None
    sample_count: int
    missing_sample_count: int
    unreconciled_drop_count: int
    unreconciled_drop_uncertain: bool
    gap_count: int
    gap_counts_by_type: dict[str, int]
    silence_intervals: list[dict]
    unreconciled_drops: list[dict]
    malformed_line_count: int
    duplicate_count: int
    reset_count: int
    rejection_count: int
    last_sample_time: str
    termination: dict | None
    termination_cause: str
    unobserved_interval_note: str | None
    durability_levels: list[str]
    read_errors: list[ReadError]
    warnings: list[str]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _gap_range(gap: Mapping[str, Any]) -> tuple[int, int] | None:
    """The sequence range a gap record claims, or None (silence, a drop whose range is not `known`)."""
    kind = gap.get("gap_type")
    if kind == _SEQUENCE_DISCONTINUITY or (kind == _PROBE_REPORTED_DROP and gap.get("range_status") == "known"):
        first, last = gap.get("first_missing_seq"), gap.get("last_missing_seq")
        if _is_int(first) and _is_int(last) and first <= last:
            return (first, last)
    return None


def _gap_identity(gap: Mapping[str, Any]) -> tuple:
    """Semantic identity of a gap record (D29): two records with the same identity describe the same gap."""
    kind, at = gap.get("gap_type"), gap.get("detected_at_seq")
    first, last = gap.get("first_missing_seq"), gap.get("last_missing_seq")
    if kind == _PROBE_REPORTED_DROP:
        return (kind, at, gap.get("dropped_count"), gap.get("range_status"), first, last)
    return (kind, at, first, last)


def _reconciliation_warnings(gaps: list[Mapping[str, Any]]) -> list[str]:
    """Recompute `reconciliation` and `reconciles_with` for every gap from the ranges and compare with what is stored.

    The Collector marks a gap only against the other gaps of the same ingest (design, "Reconciliation marking is
    computed in the same ingest"). One ingest handles one `new` Sample, and every gap it writes carries that
    Sample's `seq` as `detected_at_seq`, so gaps with the same integer `detected_at_seq` are the same ingest. A gap
    whose `detected_at_seq` is missing or not an integer has no known peers. An overlap with a gap from another
    ingest is not a disagreement. A record with the same semantic identity as the gap (an exact duplicate) is the
    same gap and is not a peer. D29.
    """
    out: list[str] = []
    for index, gap in enumerate(gaps):
        if gap.get("gap_type") == _SILENCE_TIMEOUT:
            expected, expected_with = "not_applicable", []
        else:
            at = gap.get("detected_at_seq")
            identity = _gap_identity(gap)
            same_ingest = _is_int(at)
            others = [
                (str(o.get("gap_id")), _gap_range(o))
                for j, o in enumerate(gaps)
                if same_ingest and j != index and o.get("detected_at_seq") == at and o.get("gap_type") != _SILENCE_TIMEOUT
                and _is_int(o.get("detected_at_seq")) and _gap_identity(o) != identity
            ]
            expected, expected_with = reconcile(_gap_range(gap), others)
        stored, stored_with = gap.get("reconciliation"), gap.get("reconciles_with")
        stored_ids = {str(i) for i in stored_with} if isinstance(stored_with, list) else None
        if stored != expected or stored_ids != set(expected_with):
            out.append(
                f"gap {gap.get('gap_id')!r}: stored reconciliation {stored!r} (reconciles_with {stored_with!r}) disagrees with "
                f"the ranges, which give {expected!r} (reconciles_with {expected_with!r}); the ranges were used"
            )
    return out


def summarize(result: ReadResult) -> RunSummary:
    """Summarize a `ReadResult`. Pure and deterministic; reads only `result.records` and `result.errors`.

    * Only valid, non-quarantined records are used. A quarantined or `isolated_by_repair` line counts only in
      `malformed_line_count`. `repair` records are never Samples.
    * `sample_count` counts `sample` records only. `last_sample_time` is the `ts` of the last `sample` record in
      file order (a Sample is appended only when new, so file order is sequence order), else "none received".
    * Gap numbers come from `gaps.missing_sample_count` and `gaps.unreconciled_drops` over the `gap` records;
      nothing is recounted here. `unreconciled_drop_uncertain` is true when any unreconciled drop is listed (each is
      uncertain, and an unknown or uncertain range is listed even with a count of 0).
      A malformed gap payload raises `gaps.GapRecordError`; it is not skipped, so no count is silently wrong
      (payload validation at read time is task 2.19, which will quarantine such records instead).
    * Each `silence_timeout` gap is an interval with `sample_count` "unknown". It adds to no count.
    * `duplicate_count` and `reset_count` count `sequence_anomaly` records by `anomaly`; `rejection_count` counts
      `rejection` records.
    * `termination` is the last valid `termination` record (a warning is added when there are several);
      `termination_cause` is its `cause_label`, and "unknown" when there is no record or no usable label. It is
      never inferred from anything else.
    * `unobserved_interval_note` is set when a termination record exists or a silence gap is open (no valid Sample
      follows it in file order).
    * `durability_levels` lists the distinct `durability` values of `run_metadata` records, first seen first.
    * A stored `reconciliation` that disagrees with the ranges of the gaps from the same ingest (same
      `detected_at_seq`, D29) adds a warning; the stored value is never used.
    """
    records = result.records
    by_type: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for position, record in enumerate(records):
        by_type.setdefault(str(record.get("record_type")), []).append((position, record))
    samples = by_type.get("sample", [])
    gap_entries = by_type.get("gap", [])
    gaps = [g for _, g in gap_entries]
    warnings: list[str] = []

    counts: dict[str, int] = {}
    for gap in gaps:
        kind = gap.get("gap_type")
        key = kind if isinstance(kind, str) else "<invalid>"
        counts[key] = counts.get(key, 0) + 1
        if key not in _GAP_TYPES:
            warnings.append(f"gap {gap.get('gap_id')!r} has an unrecognised gap_type {kind!r}; counted under {key!r}, in no gap number")

    missing = missing_sample_count(gaps)
    drops = [dict(d) for d in unreconciled_drops(gaps)]
    last_sample_position = samples[-1][0] if samples else -1
    last_sample_time = samples[-1][1]["sample"]["ts"] if samples else NONE_RECEIVED

    silence_intervals: list[dict] = []
    open_silence = False
    for position, gap in gap_entries:
        if gap.get("gap_type") != _SILENCE_TIMEOUT:
            continue
        silence_intervals.append(
            {"last_sample_time": gap.get("last_sample_time"), "detected_time": gap.get("detected_time"), "sample_count": "unknown"}
        )
        if position > last_sample_position:
            open_silence = True

    terminations = by_type.get("termination", [])
    termination = dict(terminations[-1][1]) if terminations else None
    if len(terminations) > 1:
        warnings.append(f"{len(terminations)} termination records found; the last one is reported")
    label = termination.get("cause_label") if termination else None
    cause = label if isinstance(label, str) and label else UNKNOWN_CAUSE

    levels: list[str] = []
    for _, meta in by_type.get("run_metadata", []):
        level = meta.get("durability")
        if isinstance(level, str) and level not in levels:
            levels.append(level)

    anomalies = [r.get("anomaly") for _, r in by_type.get("sequence_anomaly", [])]
    run_id = next((r["run_id"] for r in records if isinstance(r.get("run_id"), str)), None)
    warnings.extend(_reconciliation_warnings(gaps))

    return RunSummary(
        run_id=run_id,
        sample_count=len(samples),
        missing_sample_count=missing,
        unreconciled_drop_count=sum(d["count"] for d in drops),
        unreconciled_drop_uncertain=bool(drops),
        gap_count=len(gaps),
        gap_counts_by_type=counts,
        silence_intervals=silence_intervals,
        unreconciled_drops=drops,
        malformed_line_count=len(result.errors),
        duplicate_count=anomalies.count("duplicate"),
        reset_count=anomalies.count("reset"),
        rejection_count=len(by_type.get("rejection", [])),
        last_sample_time=last_sample_time,
        termination=termination,
        termination_cause=cause,
        unobserved_interval_note=UNOBSERVED_NOTE if (termination is not None or open_silence) else None,
        durability_levels=levels,
        read_errors=list(result.errors),
        warnings=warnings,
    )