"""Evidence writer: JSONL serialization and durable, append-only persistence.

Requirements 4.6, 4.10 to 4.15, 5.4, 5.12. Standard library plus `coroner.common` and
`coroner.evidence.schema`.

What it guarantees
* Every record is one UTF-8 (in fact ASCII, via `ensure_ascii`) JSON object on one line,
  terminated by exactly one newline character, with no raw newline inside it (Requirement 5.4).
* `EvidenceWriter.append` checks every record BEFORE any byte is written: it must be a JSON
  object that passes `validate_record` (the envelope for every type, plus the full nested
  Sample for `sample` records) and must belong in the target file. One bad record rejects the
  whole batch and the file is untouched. The payloads of `run_metadata`, `gap`,
  `sequence_anomaly`, `rejection`, `repair`, and `termination` records are NOT validated
  beyond the envelope: that is a known, tracked gap (task 2.19), not a guarantee.
* Files are only ever opened in binary append mode. Nothing is truncated, rewritten, or
  deleted here.
* Process_Durable: write and flush to the operating system. Crash_Durable: also `fsync` the
  file, and `fsync` the directory when the file was created (and again on a retry, if the earlier attempt that created it failed before the directory was synced), before returning. A failing
  step raises `OSError` and no result is returned, so a caller can never acknowledge
  something that was not persisted. `AppendResult.durability` reports what was actually
  done, not what was configured.
* `fsync` and `fsync_dir` are injectable so tests observe the calls without a host crash.
* Repair-before-append: `append` refuses (TailRepairRequired, nothing written) when the file does not end
  with a newline (a record could be glued onto a partial line) or when its last line, though terminated,
  is not a JSON object (design D27: a malformed tail is repaired and recorded before anything is added).
  `append_bytes_durably`, which the
  repair itself uses, is not guarded.
* No atomicity is claimed. A batch is one write call, but a failed write, flush, or fsync can leave part
  of the batch, even part of a line, in the file. Only a normal return means every line was written
  and, under Crash_Durable, synced. After an OSError the file may end in a partial line, and the
  caller must run tail repair before appending again.

`EvidenceWriter` is not thread-safe: callers hold the per-run lock.
"""
from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from coroner.common.envparse import ConfigError
from coroner.evidence.schema import ValidationIssue, validate_record, validate_run_id

__all__ = [
    "AppendResult",
    "ConfigError",
    "Durability",
    "EvidenceWriter",
    "RecordError",
    "REJECTIONS_STEM",
    "TailRepairRequired",
    "TAIL_LINE_CAP",
    "fsync_directory",
    "is_json_object",
    "parse_durability",
    "rfind_newline",
    "serialize_record",
]

REJECTIONS_STEM = "_rejections"
FILE_SUFFIX = ".jsonl"
TAIL_LINE_CAP = 2 * 1024 * 1024  # a last line longer than this is never parsed and is invalid (D28); repair imports this, so both agree
_TAIL_CHUNK = 64 * 1024


class Durability(str, Enum):
    PROCESS = "process"
    CRASH = "crash"


class RecordError(ValueError):
    """A record (or batch) cannot be written. Nothing was written. `issues` lists schema problems."""

    def __init__(self, message: str, issues: Iterable[ValidationIssue] = ()) -> None:
        super().__init__(message)
        self.issues: tuple[ValidationIssue, ...] = tuple(issues)


class TailRepairRequired(RuntimeError):
    """The file does not end with a newline, so appending would glue a record onto a partial line.

    Run `coroner.evidence.repair.repair_file` first. Nothing was written.
    """


def parse_durability(raw: str | None) -> Durability:
    """Map `CORONER_DURABILITY` to a level: unset or empty is `crash`; only `process` and `crash` are valid."""
    if raw is None or raw == "":
        return Durability.CRASH
    if raw == "process":
        return Durability.PROCESS
    if raw == "crash":
        return Durability.CRASH
    raise ConfigError(f"invalid CORONER_DURABILITY={raw!r} (expected 'process' or 'crash')")


_NO_BAD_KEY = object()


def _first_non_string_key(value: object) -> object:
    """Return a non-string dict key found anywhere inside `value`, else a sentinel. Iterative, cycle-safe."""
    stack = [value]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if isinstance(node, (dict, list, tuple)):
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, dict):
                for key in node:
                    if not isinstance(key, str):
                        return key
                stack.extend(node.values())
            else:
                stack.extend(node)
    return _NO_BAD_KEY


def serialize_record(record: Mapping[str, Any]) -> bytes:
    """One record as one line: compact ASCII JSON plus a single `\\n`. Raises RecordError if it cannot be.

    `ensure_ascii=True` escapes U+2028, U+2029 and every non-ASCII character, `allow_nan=False`
    rejects NaN and infinity, and non-string keys are rejected instead of being silently
    turned into strings, so reading the line back gives the same values.
    """
    if not isinstance(record, Mapping):
        raise RecordError(f"record must be a mapping (JSON object), got {type(record).__name__}")
    top = dict(record)
    bad_key = _first_non_string_key(top)
    if bad_key is not _NO_BAD_KEY:
        raise RecordError(f"record has the non-string key {bad_key!r}; JSON object keys must be strings")
    try:
        text = json.dumps(top, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise RecordError(f"record cannot be serialized as JSON: {exc}") from exc
    data = text.encode("ascii")
    if b"\n" in data or b"\r" in data:  # cannot happen with ensure_ascii; checked because Requirement 5.4 depends on it
        raise RecordError("serialized record contains a raw newline")
    return data + b"\n"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


def is_json_object(line: bytes) -> bool:
    try:
        value = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return isinstance(value, dict)


def rfind_newline(handle: Any, end: int) -> int:
    """Offset of the last newline strictly before `end`, or -1, scanning backward in chunks."""
    position = end
    while position > 0:
        low = max(0, position - _TAIL_CHUNK)
        handle.seek(low)
        index = handle.read(position - low).rfind(b"\n")
        if index >= 0:
            return low + index
        position = low
    return -1


def fsync_directory(directory: Path) -> None:
    """`fsync` a directory so a newly created file's entry is durable. A documented no-op on Windows,
    where a directory cannot be opened for fsync (the Collector always runs in a Linux container)."""
    if sys.platform == "win32":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class AppendResult:
    path: Path
    record_count: int
    byte_count: int
    created: bool
    fsynced_file: bool
    fsynced_directory: bool

    @property
    def durability(self) -> Durability:
        """The level actually achieved: `crash` only if the file was fsynced (and the directory too, if created)."""
        if self.record_count and self.fsynced_file and (self.fsynced_directory or not self.created):
            return Durability.CRASH
        return Durability.PROCESS


class EvidenceWriter:
    def __init__(
        self,
        directory: Path,
        durability: Durability,
        *,
        fsync: Callable[[int], None] = os.fsync,
        fsync_dir: Callable[[Path], None] = fsync_directory,
    ) -> None:
        if not isinstance(durability, Durability):
            raise TypeError(f"durability must be a Durability, got {type(durability).__name__}")
        self._directory = Path(directory)
        self._durability = durability
        self._fsync = fsync
        self._fsync_dir = fsync_dir
        # Files this writer created whose directory entry has not yet been fsynced successfully.
        # A first append that fails after creating the file must not let the retry skip the directory fsync.
        self._dir_sync_pending: set[Path] = set()
        self._closed = False

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def durability(self) -> Durability:
        """The configured level. `AppendResult.durability` is what a given append actually did."""
        return self._durability

    def path_for(self, run_id: str) -> Path:
        """`<directory>/<run_id>.jsonl`, or `_rejections.jsonl` for the reserved name `_rejections`."""
        if run_id == REJECTIONS_STEM:
            return self._directory / (REJECTIONS_STEM + FILE_SUFFIX)
        issue = validate_run_id(run_id)
        if issue is not None:
            raise ValueError(issue.reason)
        return self._directory / (run_id + FILE_SUFFIX)

    def append(self, run_id: str, records: Iterable[Mapping[str, Any]]) -> AppendResult:
        """Validate every record, then write all of them with one write, flush, and the durability steps.

        Raises RecordError (nothing written) for an invalid or misplaced record, ValueError for an
        invalid `run_id`, and OSError if a write, flush, or fsync step fails (a partial line may then
        be left in the file; the caller must repair it before the next append).
        """
        self._require_open()
        path = self.path_for(run_id)
        if isinstance(records, (str, bytes, bytearray, Mapping)):
            raise RecordError("records must be a sequence of mappings, not a single record or text")
        items = list(records)
        lines: list[bytes] = []
        for index, record in enumerate(items):
            self._check_record(run_id, index, record)
            try:
                lines.append(serialize_record(record))
            except RecordError as exc:
                raise RecordError(f"record {index}: {exc}") from exc
        if not lines:
            return AppendResult(path, 0, 0, False, False, False)
        problem = self._tail_problem(path)  # repair-before-append (design D26, D27); stateless, read from the file
        if problem == "unterminated":
            raise TailRepairRequired(f"{path.name} does not end with a newline; run repair_file before appending")
        if problem is not None:
            raise TailRepairRequired(f"{path.name} ends with a line that is not a JSON object; run repair_file before appending")
        data = b"".join(lines)
        created, fsynced_file, fsynced_dir = self._write(path, data)
        return AppendResult(path, len(lines), len(data), created, fsynced_file, fsynced_dir)

    def append_bytes_durably(self, path: Path, data: bytes) -> None:
        """Append raw bytes (for example an isolating newline plus a repair record) at the configured level.

        The path must be inside the writer's directory. Nothing is validated here: the caller supplies
        complete lines, for example from `serialize_record`.
        """
        self._require_open()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"data must be bytes, got {type(data).__name__}")
        target = Path(path)
        root = self._directory.resolve()
        parent = target.parent.resolve()
        if parent != root and root not in parent.parents:
            raise ValueError(f"{target} is outside the evidence directory {self._directory}")
        if len(data) == 0:
            return
        self._write(target, bytes(data))

    def close(self) -> None:
        """Mark the writer closed. No file handle is kept between appends. Safe to call twice."""
        self._closed = True

    @staticmethod
    def _tail_problem(path: Path) -> str | None:
        """None when `path` is missing, empty, or ends with a terminated JSON-object line; else why appending is refused.

        "unterminated": the last byte is not a newline. "invalid_last_line": the last line is terminated but is
        not a UTF-8 JSON object, or is longer than TAIL_LINE_CAP. The validity test is the one `repair.inspect_tail`
        uses, so a file that `repair_file` calls clean is never refused here (a test checks that they agree).
        Only the last line is read, backward in chunks. `io.open`, not the module-level `open`.
        """
        try:
            with io.open(path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                if size == 0:
                    return None
                handle.seek(size - 1)
                if handle.read(1) != b"\n":
                    return "unterminated"
                end = size - 1  # exclusive end of the last line's content
                start = rfind_newline(handle, end) + 1
                if end - start > TAIL_LINE_CAP:
                    return "invalid_last_line"
                handle.seek(start)
                return None if is_json_object(handle.read(end - start)) else "invalid_last_line"
        except FileNotFoundError:
            return None

    def _require_open(self) -> None:
        if self._closed:
            raise ValueError("EvidenceWriter is closed")

    @staticmethod
    def _check_record(run_id: str, index: int, record: object) -> None:
        issues = validate_record(record)
        if issues:
            detail = "; ".join(f"{i.field}: {i.reason_code}" for i in issues[:5])
            raise RecordError(f"record {index} is invalid ({detail})", issues)
        assert isinstance(record, Mapping)
        if run_id == REJECTIONS_STEM:
            if record.get("record_type") != "rejection" or record.get("run_id") is not None:
                raise RecordError(f"record {index} does not belong in {REJECTIONS_STEM}.jsonl: only rejection records with run_id null do")
        elif record.get("run_id") != run_id:
            raise RecordError(f"record {index} has run_id {record.get('run_id')!r}, expected {run_id!r}")

    def _write(self, path: Path, data: bytes) -> tuple[bool, bool, bool]:
        """Append `data`. Returns (created, fsynced_file, fsynced_directory). Binary append mode only."""
        created = not path.exists()
        crash = self._durability is Durability.CRASH
        if created and crash:
            self._dir_sync_pending.add(path)
        needs_dir_sync = crash and path in self._dir_sync_pending
        fsynced_file = False
        with open(path, "ab") as handle:
            handle.write(data)
            handle.flush()
            if self._durability is Durability.CRASH:
                self._fsync(handle.fileno())
                fsynced_file = True
        fsynced_dir = False
        if needs_dir_sync:
            self._fsync_dir(path.parent)
            fsynced_dir = True
            self._dir_sync_pending.discard(path)
        return created, fsynced_file, fsynced_dir