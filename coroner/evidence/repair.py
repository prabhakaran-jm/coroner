"""Append-only repair of an evidence file's last line (Requirements 5.5 to 5.8; design D12, D19, D26).

A write that failed part-way, or a crash, can leave the last line of a JSONL file incomplete. Before
any further record is appended to that file the tail must be isolated so a new record starts on a
fresh line, and what was found there must be preserved:

* The damaged bytes are never altered, moved, or deleted. Nothing is ever truncated. They stay in the
  file as a Malformed_Line (the Evidence_Reader quarantines it) and, up to 1 MiB (FRAGMENT_CAP), are copied
  base64-encoded into a `repair` record (`fragment_len` always holds the true length).
* When the tail has no newline, ONE invalidating NUL byte (INVALIDATING_MARKER), the isolating newline and
  the repair record are written in ONE durable append at the writer's durability level, so the repair is
  persisted before any later append. The NUL (D30) makes the original line invalid JSON as soon as it
  persists, so an interrupted repair can never leave an unacknowledged Sample that reads as valid. The
  physical isolated line is therefore the original fragment plus one NUL; it is NOT byte-identical to the
  fragment. `fragment_len` and `fragment_b64` describe the fragment as this repair attempt found it, without
  its own marker. If an earlier attempt persisted only its marker, the next attempt finds `fragment + NUL`
  as its fragment, records that earlier marker as part of it, and appends another NUL. A newline-terminated
  invalid last line gets no marker and no newline (`none_needed_already_terminated`).
* Repair is idempotent: once the tail is clean (the file ends with a newline and the last line is a
  JSON object) a second call changes nothing. If a repair was interrupted, the next call finds the
  partial repair record as the new tail and isolates that, so no byte is lost.
* Only the last line is examined. Damaged lines in the middle of a file are the reader's concern.
* Only tail repair lives here. Recovering missing gap records (task 3.5) and the startup scan and gating
  of dirty runs (task 3.2) do not.

Decisions: a last line counts as valid only if it is a UTF-8 JSON object (a bare number or an array is
not a record); `NaN` and `Infinity` literals are not valid. The `repair` record is written for the
run named by the file; for `_rejections.jsonl` its `run_id` is null (the design allows null only in
that file). Standard library plus `coroner.evidence.writer` and `coroner.common`.
"""
from __future__ import annotations

import base64
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coroner.common.timeutil import parse_utc_ms
from coroner.evidence.schema import SCHEMA_VERSION, validate_run_id
from coroner.evidence.writer import (
    REJECTIONS_STEM,
    TAIL_LINE_CAP,
    EvidenceWriter,
    is_json_object,
    rfind_newline,
    serialize_record,
)

__all__ = ["FRAGMENT_CAP", "INVALIDATING_MARKER", "TailInspection", "inspect_tail", "repair_file"]

FRAGMENT_CAP = 1024 * 1024  # bytes of a fragment copied into the repair record
# D30: appended after an UNTERMINATED fragment, before the isolating newline. A NUL after a complete JSON value
# is "Extra data" to the JSON parser, so the line can no longer read as a valid record even if the repair stops
# right after the marker or after the newline. NUL is valid UTF-8, so the reader classifies it as invalid JSON.
INVALIDATING_MARKER = b"\x00"
CHUNK = 64 * 1024  # backward scan and newline counting chunk
_FILE_SUFFIX = ".jsonl"


@dataclass(frozen=True)
class TailInspection:
    """What the end of a file looks like. `last_line_no` is 0 for an empty file (nothing to repair).

    `last_line` excludes its newline and holds at most FRAGMENT_CAP bytes (the fragment to copy);
    `last_line_len` is the true length. At most TAIL_LINE_CAP (2 MiB, D28) bytes are read to judge validity;
    a line longer than TAIL_LINE_CAP is never parsed and is not valid. The limit exceeds FRAGMENT_CAP so a
    repair record carrying a 1 MiB fragment (about 1.4 MB after base64) is itself a valid tail.
    """

    ends_with_newline: bool
    last_line: bytes
    last_line_valid_json: bool
    last_line_no: int
    last_line_len: int = 0


def _count_newlines(handle, end: int) -> int:
    handle.seek(0)
    remaining, total = end, 0
    while remaining > 0:
        chunk = handle.read(min(CHUNK, remaining))
        if not chunk:
            break
        total += chunk.count(b"\n")
        remaining -= len(chunk)
    return total


def inspect_tail(path: Path) -> TailInspection:
    """Read the end of `path` without modifying it. Raises FileNotFoundError or another OSError as usual."""
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        if size == 0:
            return TailInspection(True, b"", True, 0, 0)
        handle.seek(size - 1)
        ends_with_newline = handle.read(1) == b"\n"
        end = size - 1 if ends_with_newline else size  # exclusive end of the last line's content
        start = rfind_newline(handle, end) + 1
        length = end - start
        handle.seek(start)
        head = handle.read(min(length, TAIL_LINE_CAP))  # bounded: never more than TAIL_LINE_CAP bytes
        valid = length <= TAIL_LINE_CAP and is_json_object(head)
        line = head[:FRAGMENT_CAP]
        line_no = _count_newlines(handle, start) + 1
    return TailInspection(ends_with_newline, line, valid, line_no, length)


def _run_id_for(path: Path) -> str | None:
    name = path.name
    if not name.endswith(_FILE_SUFFIX):
        raise ValueError(f"{name} is not an evidence file (expected <run_id>{_FILE_SUFFIX})")
    stem = name[: -len(_FILE_SUFFIX)]
    if stem == REJECTIONS_STEM:
        return None
    issue = validate_run_id(stem)
    if issue is not None:
        raise ValueError(f"{name} is not an evidence file name: {issue.reason}")
    return stem


def repair_file(path: Path, writer: EvidenceWriter, now: Callable[[], str]) -> Mapping[str, Any] | None:
    """Isolate and record a damaged last line. Returns the repair record written, or None if nothing was needed.

    `now` returns the current UTC time as `YYYY-MM-DDTHH:mm:ss.sssZ` (the record's `recv_time`).
    The file is only ever appended to, through `writer.append_bytes_durably`, so an OSError from a
    failed write, flush, or fsync propagates and no success is reported; a partial repair is simply
    repaired again by the next call. Raises ValueError if the file name is not an evidence file name.
    """
    path = Path(path)
    run_id = _run_id_for(path)
    try:
        tail = inspect_tail(path)
    except FileNotFoundError:
        return None
    if tail.last_line_no == 0:
        return None
    if tail.ends_with_newline:
        if tail.last_line_valid_json:
            return None
        action, reason = "none_needed_already_terminated", "invalid_json_last_line"
    else:
        action = "appended_newline"
        reason = "missing_trailing_newline" if tail.last_line_valid_json else "incomplete_last_line"
    recv_time = now()
    parse_utc_ms(recv_time)  # raises ValueError for a malformed time before anything is written
    record = {
        "record_type": "repair",
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "recv_time": recv_time,
        "file": path.name,
        "line_no": tail.last_line_no,
        "fragment_len": tail.last_line_len,
        "fragment_b64": base64.b64encode(tail.last_line[:FRAGMENT_CAP]).decode("ascii"),
        "fragment_truncated": tail.last_line_len > FRAGMENT_CAP,
        "reason": reason,
        "action": action,
    }
    # D30: an unterminated fragment gets one NUL marker, then the isolating newline, then the record, in ONE append
    lead = INVALIDATING_MARKER + b"\n" if action == "appended_newline" else b""
    data = lead + serialize_record(record)
    writer.append_bytes_durably(path, data)
    return record
