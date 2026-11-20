"""Tests for coroner.evidence.repair (task 2.10; requirements 5.5 to 5.8; design D12, D19, D26).

Only tail repair is covered. Replaying missing gap records (task 3.5), the startup scan and the
gate for dirty runs (task 3.2), and reading the repaired file back (tasks 2.11 and 2.14) are not.
Property 8 (bytes never lost) and Property 9 (idempotence) are tasks 2.15 and 2.16; the exhaustive
cut-point tests here are deterministic examples, not Hypothesis properties.
"""
from __future__ import annotations

import base64
import builtins
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from coroner.evidence import repair as repair_module
from coroner.evidence import writer as writer_module
from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.repair import CHUNK, FRAGMENT_CAP, TailInspection, inspect_tail, repair_file
from coroner.evidence.schema import validate_record
from coroner.evidence.writer import TAIL_LINE_CAP, Durability, EvidenceWriter, TailRepairRequired, serialize_record

NOW = "2025-01-01T12:05:00.004Z"
RECV = "2025-01-01T12:00:06.530Z"


def now() -> str:
    return NOW


def sample_record(seq: int = 0, run_id: str = "r1") -> dict:
    sample = {
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 71303168, "request_count": 9,
        "loop_delay_ms": 0.84, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }
    return {"record_type": "sample", "schema_version": 1, "run_id": run_id, "recv_time": RECV, "sample": sample}


def good_lines(count: int, run_id: str = "r1") -> bytes:
    return b"".join(serialize_record(sample_record(i, run_id)) for i in range(count))


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def fsync(self, fd: int) -> None:
        self.events.append(("fsync_file", os.fstat(fd).st_size))

    def fsync_dir(self, path: Path) -> None:
        self.events.append(("fsync_dir", Path(path)))


def make(tmp_path: Path, level: Durability = Durability.CRASH) -> tuple[EvidenceWriter, Recorder, Path]:
    directory = tmp_path / "evidence"
    directory.mkdir()
    recorder = Recorder()
    return EvidenceWriter(directory, level, fsync=recorder.fsync, fsync_dir=recorder.fsync_dir), recorder, directory


def put(directory: Path, data: bytes, name: str = "r1.jsonl") -> Path:
    path = directory / name
    path.write_bytes(data)
    return path


def last_json(path: Path) -> dict:
    return json.loads(path.read_bytes().splitlines()[-1])


class SpyFile:
    def __init__(self, real, log: list, limit: int | None = None, fail_flush: bool = False) -> None:
        self._real, self._log, self._limit, self._fail_flush = real, log, limit, fail_flush

    def __enter__(self) -> "SpyFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        self._log.append(("write", len(data)))
        if self._limit is None:
            return self._real.write(data)
        self._real.write(data[: self._limit])  # a short write followed by an error, like a full disk
        self._real.flush()
        raise OSError("no space left on device")

    def flush(self) -> None:
        self._log.append(("flush",))
        if self._fail_flush:
            raise OSError("flush failed")
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()


def spy_open(monkeypatch: pytest.MonkeyPatch, log: list, **kwargs: object) -> None:
    monkeypatch.setattr(writer_module, "open", lambda p, m: SpyFile(builtins.open(p, m), log, **kwargs), raising=False)


# --- inspect_tail ---------------------------------------------------------------------


def test_inspect_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inspect_tail(tmp_path / "nope.jsonl")


def test_inspect_empty_file_has_nothing_to_repair(tmp_path: Path) -> None:
    path = put(tmp_path, b"")
    assert inspect_tail(path) == TailInspection(True, b"", True, 0, 0)


def test_inspect_clean_file(tmp_path: Path) -> None:
    data = good_lines(3)
    tail = inspect_tail(put(tmp_path, data))
    last = data.splitlines()[-1]
    assert tail == TailInspection(True, last, True, 3, len(last))


def test_inspect_incomplete_last_line(tmp_path: Path) -> None:
    data = good_lines(2) + b'{"record_type":"sample","sche'
    tail = inspect_tail(put(tmp_path, data))
    assert (tail.ends_with_newline, tail.last_line, tail.last_line_valid_json, tail.last_line_no) == (False, b'{"record_type":"sample","sche', False, 3)


def test_inspect_valid_json_object_without_newline(tmp_path: Path) -> None:
    tail = inspect_tail(put(tmp_path, good_lines(1) + b'{"a":1}'))
    assert (tail.ends_with_newline, tail.last_line, tail.last_line_valid_json, tail.last_line_no) == (False, b'{"a":1}', True, 2)


@pytest.mark.parametrize(
    "line",
    [b'{"a":', b"", b"5", b"[1]", b'"x"', b"null", b"true", b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}',
     b"\xff\xfe", b'{"a":"\xff"}', b"not json", b'{"a":1}}', b"{}{}", b'{"a":1} x'],
)
@pytest.mark.parametrize("terminated", [True, False])
def test_inspect_treats_anything_but_a_json_object_as_invalid(tmp_path: Path, line: bytes, terminated: bool) -> None:
    if line == b"" and not terminated:
        # Nothing after the last newline: the tail is just the previous, valid record.
        tail = inspect_tail(put(tmp_path, good_lines(1)))
        assert tail.ends_with_newline and tail.last_line_valid_json and tail.last_line_no == 1
        return
    data = good_lines(1) + line + (b"\n" if terminated else b"")
    tail = inspect_tail(put(tmp_path, data))
    assert tail.ends_with_newline is terminated and tail.last_line == line and not tail.last_line_valid_json
    assert tail.last_line_no == 2 and tail.last_line_len == len(line)


@pytest.mark.parametrize("line", [b"{}", b'{"a":1}', b'{"a":[1,2,{"b":null}]}', b'  {"a":1}  ', b'{"a":1}\r'])
def test_inspect_accepts_json_objects(tmp_path: Path, line: bytes) -> None:
    assert inspect_tail(put(tmp_path, good_lines(1) + line + b"\n")).last_line_valid_json


def test_inspect_a_file_of_one_newline_and_blank_lines(tmp_path: Path) -> None:
    assert inspect_tail(put(tmp_path, b"\n")) == TailInspection(True, b"", False, 1, 0)
    tail = inspect_tail(put(tmp_path, b'{"a":1}\n\n', "b.jsonl"))
    assert (tail.last_line, tail.last_line_no, tail.last_line_valid_json) == (b"", 2, False)


def test_inspect_line_numbers_count_every_newline_before_the_last_line(tmp_path: Path) -> None:
    data = good_lines(1000) + b"partial"
    assert inspect_tail(put(tmp_path, data)).last_line_no == 1001
    assert inspect_tail(put(tmp_path, good_lines(1000), "b.jsonl")).last_line_no == 1000


@pytest.mark.parametrize("offset", [-2, -1, 0, 1, 2])
def test_inspect_handles_a_newline_at_every_position_around_the_chunk_boundary(tmp_path: Path, offset: int) -> None:
    """The last newline sits at distances CHUNK + offset from the end, so the backward scan crosses chunk edges."""
    head = b"x" * 10 + b"\n"
    tail_line = b'{"k":"' + b"v" * (CHUNK + offset - 10) + b'"}'
    data = head + tail_line
    tail = inspect_tail(put(tmp_path, data))
    assert tail.last_line == tail_line and tail.last_line_no == 2 and not tail.ends_with_newline
    assert tail.last_line_valid_json and tail.last_line_len == len(tail_line)


def test_inspect_last_line_spanning_many_chunks_and_a_long_history(tmp_path: Path) -> None:
    long_line = b'{"k":"' + b"v" * (CHUNK * 3 + 17) + b'"}'
    data = good_lines(500) + long_line + b"\n"
    tail = inspect_tail(put(tmp_path, data))
    assert tail.last_line == long_line and tail.last_line_valid_json and tail.last_line_no == 501


def test_inspect_does_not_modify_the_file(tmp_path: Path) -> None:
    path = put(tmp_path, good_lines(3) + b"partial")
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    inspect_tail(path)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_inspect_caps_a_huge_fragment_and_reports_its_true_length(tmp_path: Path) -> None:
    size = 3 * FRAGMENT_CAP + 5
    data = good_lines(1) + b"z" * size
    tail = inspect_tail(put(tmp_path, data))
    assert len(tail.last_line) == FRAGMENT_CAP and tail.last_line_len == size and not tail.last_line_valid_json
    assert tail.last_line == b"z" * FRAGMENT_CAP


def test_inspect_never_parses_a_line_longer_than_the_cap_even_if_it_is_valid_json(tmp_path: Path) -> None:
    big = b'{"k":"' + b"v" * (TAIL_LINE_CAP + 10) + b'"}'
    tail = inspect_tail(put(tmp_path, big + b"\n"))
    assert tail.last_line_len == len(big) and not tail.last_line_valid_json
    exactly = b'{"k":"' + b"v" * (TAIL_LINE_CAP - 8) + b'"}'
    assert len(exactly) == TAIL_LINE_CAP
    assert inspect_tail(put(tmp_path, exactly + b"\n", "b.jsonl")).last_line_valid_json


# --- repair_file: nothing to do --------------------------------------------------------


def test_missing_file_is_nothing_to_repair(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    assert repair_file(directory / "r1.jsonl", w, now) is None
    assert not (directory / "r1.jsonl").exists() and recorder.events == []


def test_empty_file_is_nothing_to_repair(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    path = put(directory, b"")
    assert repair_file(path, w, now) is None
    assert path.read_bytes() == b"" and recorder.events == []


def test_clean_file_is_left_byte_identical_and_nothing_is_synced(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    data = good_lines(5)
    path = put(directory, data)
    mtime = path.stat().st_mtime_ns
    assert repair_file(path, w, now) is None
    assert path.read_bytes() == data and path.stat().st_mtime_ns == mtime and recorder.events == []


# --- repair_file: the three damaged-tail cases ------------------------------------------

REQUIRED_KEYS = {"record_type", "schema_version", "run_id", "recv_time", "file", "line_no", "fragment_len",
                 "fragment_b64", "fragment_truncated", "reason", "action"}


def decode(record: dict) -> bytes:
    return base64.b64decode(record["fragment_b64"], validate=True)


def test_incomplete_last_line_is_isolated_and_preserved(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    complete = good_lines(3)
    fragment = b'{"record_type":"sample","schema_version":1,"run_id":"r1","rec'
    path = put(directory, complete + fragment)
    record = repair_file(path, w, now)
    assert record is not None and set(record) == REQUIRED_KEYS
    assert record["record_type"] == "repair" and record["schema_version"] == 1 and record["run_id"] == "r1"
    assert record["recv_time"] == NOW and record["file"] == "r1.jsonl"
    assert record["line_no"] == 4 and record["fragment_len"] == len(fragment) and record["fragment_truncated"] is False
    assert record["reason"] == "incomplete_last_line" and record["action"] == "appended_newline"
    assert decode(record) == fragment
    after = path.read_bytes()
    # D30: original bytes untouched, then one NUL marker and one isolating newline
    assert after == complete + fragment + b"\x00\n" + serialize_record(record)
    assert after.endswith(b"\n") and last_json(path) == record
    assert validate_record(record) == []


def test_valid_json_without_a_newline_is_isolated_not_accepted(tmp_path: Path) -> None:
    """D19: a record that lacked its newline was never acknowledged, so it is isolated like any fragment."""
    w, _, directory = make(tmp_path)
    fragment = serialize_record(sample_record(7)).rstrip(b"\n")
    path = put(directory, good_lines(1) + fragment)
    record = repair_file(path, w, now)
    assert record["reason"] == "missing_trailing_newline" and record["action"] == "appended_newline"
    assert record["line_no"] == 2 and decode(record) == fragment and record["fragment_len"] == len(fragment)
    assert path.read_bytes() == good_lines(1) + fragment + b"\x00\n" + serialize_record(record)  # D30 marker


def test_a_terminated_but_invalid_last_line_gets_a_record_and_no_extra_newline(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    fragment = b'{"truncated":'
    original = good_lines(2) + fragment + b"\n"
    path = put(directory, original)
    record = repair_file(path, w, now)
    assert record["reason"] == "invalid_json_last_line" and record["action"] == "none_needed_already_terminated"
    assert record["line_no"] == 3 and decode(record) == fragment
    assert path.read_bytes() == original + serialize_record(record)  # no isolating newline was needed


@pytest.mark.parametrize(
    "fragment, terminated",
    [(b"\xff\xfe\x00 garbage", False), (b"\xff\xfe\x00 garbage", True), (b"[1,2,3]", False), (b"[1,2,3]", True),
     (b'{"a":NaN}', False), (b"5", True), (b"", True), (b"\x00" * 50, False), (b'{"a":"\xc3"}', False), (b"{} trailing", True)],
)
def test_odd_tails_are_preserved_byte_for_byte(tmp_path: Path, fragment: bytes, terminated: bool) -> None:
    w, _, directory = make(tmp_path)
    original = good_lines(2) + fragment + (b"\n" if terminated else b"")
    path = put(directory, original)
    record = repair_file(path, w, now)
    assert record is not None and decode(record) == fragment and record["fragment_len"] == len(fragment)
    assert path.read_bytes().startswith(original)
    assert repair_file(path, w, now) is None


def test_repair_is_idempotent(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    path = put(directory, good_lines(2) + b'{"partial"')
    first = repair_file(path, w, now)
    after_first = path.read_bytes()
    recorder.events.clear()
    for _ in range(3):
        assert repair_file(path, w, now) is None
    assert path.read_bytes() == after_first and recorder.events == []
    assert first is not None and after_first.count(b'"record_type":"repair"') == 1


def test_originals_stay_an_unmodified_prefix_and_the_file_only_grows(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    original = good_lines(10) + b'{"half'
    path = put(directory, original)
    repair_file(path, w, now)
    after = path.read_bytes()
    assert after.startswith(original) and len(after) > len(original)
    assert after[: len(good_lines(10))] == good_lines(10)  # every complete record is untouched


def test_line_number_is_the_one_based_line_of_the_fragment(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    for count in (0, 1, 2, 9):
        path = put(directory, good_lines(count) + b"frag", f"run{count}.jsonl")
        assert repair_file(path, w, now)["line_no"] == count + 1


def test_long_files_are_handled(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path, Durability.PROCESS)
    path = put(directory, good_lines(20000) + b"{part")
    assert repair_file(path, w, now)["line_no"] == 20001


# --- fragment cap -----------------------------------------------------------------------


def test_a_fragment_over_one_mebibyte_is_truncated_in_the_record_but_never_in_the_file(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    fragment = b"q" * (FRAGMENT_CAP + 10)
    original = good_lines(1) + fragment
    path = put(directory, original)
    record = repair_file(path, w, now)
    assert record["fragment_len"] == FRAGMENT_CAP + 10 and record["fragment_truncated"] is True
    assert decode(record) == fragment[:FRAGMENT_CAP]
    assert path.read_bytes().startswith(original + b"\x00\n")  # the whole fragment is still in the file


def test_a_fragment_of_exactly_one_mebibyte_is_not_marked_truncated(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    fragment = b"q" * FRAGMENT_CAP
    record = repair_file(put(directory, fragment), w, now)
    assert record["fragment_truncated"] is False and decode(record) == fragment and record["line_no"] == 1


# --- file names and the record's run id --------------------------------------------------


def test_rejections_file_gets_a_repair_record_with_a_null_run_id(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    path = put(directory, b'{"half', "_rejections.jsonl")
    record = repair_file(path, w, now)
    assert record["run_id"] is None and record["file"] == "_rejections.jsonl"


def test_a_null_run_id_repair_record_passes_the_schema_but_belongs_only_in_the_rejections_file(tmp_path: Path) -> None:
    """Design D27: the schema allows the null; the reader and the writer decide which file may hold it."""
    w, _, directory = make(tmp_path)
    record = repair_file(put(directory, b"x", "_rejections.jsonl"), w, now)
    assert validate_record(record) == []
    with pytest.raises(writer_module.RecordError):
        w.append("r1", [record])  # not appendable to a run file
    with pytest.raises(writer_module.RecordError):
        w.append("_rejections", [record])  # `append` takes only rejection records; repairs go through repair_file


@pytest.mark.parametrize("name", ["notes.txt", "r1.json", "r1.JSONL", "bad name.jsonl", "_other.jsonl", ".hidden.jsonl", "r1", ".jsonl"])
def test_a_file_that_is_not_an_evidence_file_is_refused_and_untouched(tmp_path: Path, name: str) -> None:
    w, recorder, directory = make(tmp_path)
    if "/" in name:
        (directory / "a").mkdir()
    path = directory / name
    path.write_bytes(b"{partial")
    with pytest.raises(ValueError, match="evidence file"):
        repair_file(path, w, now)
    assert path.read_bytes() == b"{partial" and recorder.events == []


def test_a_path_outside_the_writer_directory_is_refused_by_the_writer(tmp_path: Path) -> None:
    w, recorder, _ = make(tmp_path)
    outside = tmp_path / "r1.jsonl"
    outside.write_bytes(b"{partial")
    with pytest.raises(ValueError, match="outside"):
        repair_file(outside, w, now)
    assert outside.read_bytes() == b"{partial" and recorder.events == []


def test_a_string_path_is_accepted(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    path = put(directory, b"{partial")
    assert repair_file(str(path), w, now) is not None  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "now", "2025-01-01T12:00:00Z", "2025-13-01T00:00:00.000Z"])
def test_a_malformed_time_is_refused_before_anything_is_written(tmp_path: Path, bad: str) -> None:
    w, recorder, directory = make(tmp_path)
    path = put(directory, b"{partial")
    with pytest.raises(ValueError):
        repair_file(path, w, lambda: bad)
    assert path.read_bytes() == b"{partial" and recorder.events == []


# --- durability: the repair is persisted before it returns -------------------------------


def test_crash_durable_repair_is_one_write_one_flush_one_file_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple] = []
    w, recorder, directory = make(tmp_path, Durability.CRASH)
    recorder.events = log
    path = put(directory, good_lines(1) + b"{partial")
    spy_open(monkeypatch, log)
    repair_file(path, w, now)
    assert [e[0] for e in log] == ["write", "flush", "fsync_file"]  # one append, persisted before returning
    assert log[2][1] == path.stat().st_size  # every repair byte was on disk when fsync ran


def test_process_durable_repair_flushes_but_never_fsyncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple] = []
    w, recorder, directory = make(tmp_path, Durability.PROCESS)
    path = put(directory, b"{partial")
    spy_open(monkeypatch, log)
    repair_file(path, w, now)
    assert [e[0] for e in log] == ["write", "flush"] and recorder.events == []


def test_a_repair_returns_only_after_the_isolating_newline_and_record_are_in_one_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple] = []
    w, _, directory = make(tmp_path)
    path = put(directory, b"{partial")
    spy_open(monkeypatch, log)
    record = repair_file(path, w, now)
    assert log[0] == ("write", 2 + len(serialize_record(record)))  # NUL marker, newline, record (D30)


# --- repair-before-append ----------------------------------------------------------------


def test_append_after_a_completed_repair_starts_on_a_fresh_line(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    original = good_lines(2) + b'{"record_type":"sample","sche'
    path = put(directory, original)
    with pytest.raises(TailRepairRequired):
        w.append("r1", [sample_record(5)])
    assert path.read_bytes() == original  # refused, nothing written
    repair = repair_file(path, w, now)
    recorder.events.clear()
    w.append("r1", [sample_record(5)])
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b""  # ends with a newline
    assert json.loads(lines[-2])["sample"]["seq"] == 5
    assert json.loads(lines[-3]) == repair
    assert lines[-4] == b'{"record_type":"sample","sche\x00'  # the fragment plus the D30 marker is its own line
    valid = [json.loads(x) for x in lines[:-1] if x != lines[-4]]
    assert len(valid) == 2 + 1 + 1  # two original records, the repair record, the new Sample


def test_the_repair_is_persisted_before_the_next_append_is_accepted(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    path = put(directory, b'{"partial')
    repair_file(path, w, now)
    repair_events = list(recorder.events)
    w.append("r1", [sample_record(1)])
    assert [e[0] for e in repair_events] == ["fsync_file"]
    assert [e[0] for e in recorder.events] == ["fsync_file", "fsync_file"]  # repair synced first, then the append
    assert recorder.events[0][1] < recorder.events[1][1]


def test_a_clean_file_needs_no_repair_before_append(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    put(directory, good_lines(2))
    w.append("r1", [sample_record(2)])
    assert len((directory / "r1.jsonl").read_bytes().splitlines()) == 3


def test_a_terminated_invalid_last_line_blocks_appends_until_it_is_repaired(tmp_path: Path) -> None:
    """Design D27: repair-before-append also covers a terminated last line that is not a JSON object."""
    w, recorder, directory = make(tmp_path)
    original = good_lines(1) + b"garbage\n"
    path = put(directory, original)
    with pytest.raises(TailRepairRequired, match="not a JSON object"):
        w.append("r1", [sample_record(1)])
    assert path.read_bytes() == original and recorder.events == []
    record = repair_file(path, w, now)
    assert record["action"] == "none_needed_already_terminated" and record["reason"] == "invalid_json_last_line"
    w.append("r1", [sample_record(1)])  # the repair record is now the valid tail
    lines = path.read_bytes().splitlines()
    assert lines[1] == b"garbage" and json.loads(lines[2])["record_type"] == "repair" and len(lines) == 4
    assert repair_file(path, w, now) is None


@pytest.mark.parametrize(
    "tail",
    [b"garbage\n", b"[1]\n", b"5\n", b"\n", b"\xff\n", b'{"a":NaN}\n', b'{"a":1}\n', b" {}\n", b"{}\n", b"{}\r\n",
     b"x" * (FRAGMENT_CAP + 1) + b"\n", b'{"a":"' + b"x" * (FRAGMENT_CAP - 10) + b'"}\n', b"{" + b" " * FRAGMENT_CAP + b"}\n",
     b"x" * (TAIL_LINE_CAP + 1) + b"\n", b'{"a":"' + b"x" * (TAIL_LINE_CAP - 10) + b'"}\n', b'{"a":"' + b"x" * (TAIL_LINE_CAP - 8) + b'"}\n',
     b'{"a":"' + b"x" * (TAIL_LINE_CAP - 7) + b'"}\n'],
    ids=lambda tail: f"{tail[:8]!r}_{len(tail)}",
)
def test_the_writer_guard_and_repair_agree_on_whether_a_tail_is_clean(tmp_path: Path, tail: bytes) -> None:
    w, _, directory = make(tmp_path)
    path = put(directory, good_lines(1) + tail)
    inspection = inspect_tail(path)
    repair_says_clean = inspection.ends_with_newline and inspection.last_line_valid_json
    assert (w._tail_problem(path) is None) == repair_says_clean
    if not repair_says_clean:
        repair_file(path, w, now)
        assert w._tail_problem(path) is None  # after a repair the tail is a valid repair record


def test_a_file_that_ends_in_a_blank_line_is_refused_and_then_repaired(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    path = put(directory, good_lines(1) + b"\n")
    with pytest.raises(TailRepairRequired):
        w.append("r1", [sample_record(1)])
    repair_file(path, w, now)
    w.append("r1", [sample_record(1)])


# --- failures during repair --------------------------------------------------------------


def test_a_failing_fsync_raises_and_the_bytes_already_written_stay(tmp_path: Path) -> None:
    def bad(fd: int) -> None:
        raise OSError("fsync failed")

    directory = tmp_path / "evidence"
    directory.mkdir()
    w = EvidenceWriter(directory, Durability.CRASH, fsync=bad, fsync_dir=lambda p: None)
    original = good_lines(1) + b"{partial"
    path = put(directory, original)
    with pytest.raises(OSError, match="fsync failed"):
        repair_file(path, w, now)
    after = path.read_bytes()
    assert after.startswith(original + b"\x00\n") and after.endswith(b"\n")  # nothing lost, nothing truncated


def test_after_a_failed_fsync_a_retry_reports_nothing_to_do_so_callers_must_not_treat_that_as_durable(tmp_path: Path) -> None:
    """Concern for task 3.2: the file is already clean, so `repair_file` returns None without syncing again."""
    state = {"fail": True}

    def flaky(fd: int) -> None:
        if state["fail"]:
            raise OSError("fsync failed")

    directory = tmp_path / "evidence"
    directory.mkdir()
    w = EvidenceWriter(directory, Durability.CRASH, fsync=flaky, fsync_dir=lambda p: None)
    path = put(directory, b"{partial")
    with pytest.raises(OSError):
        repair_file(path, w, now)
    state["fail"] = False
    assert repair_file(path, w, now) is None


def test_a_failed_flush_raises_and_never_reaches_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple] = []
    w, recorder, directory = make(tmp_path)
    recorder.events = log
    path = put(directory, b"{partial")
    spy_open(monkeypatch, log, fail_flush=True)
    with pytest.raises(OSError, match="flush failed"):
        repair_file(path, w, now)
    assert [e[0] for e in log] == ["write", "flush"]


def test_a_write_that_fails_before_any_byte_changes_nothing_and_a_retry_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w, recorder, directory = make(tmp_path)
    original = good_lines(1) + b"{partial"
    path = put(directory, original)
    with monkeypatch.context() as patch:
        spy_open(patch, [], limit=0)
        with pytest.raises(OSError):
            repair_file(path, w, now)
    assert path.read_bytes() == original and recorder.events == []
    assert repair_file(path, w, now) is not None and repair_file(path, w, now) is None


FRAGMENTS = {
    "truncated json": b'{"record_type":"sample","schema_version":1,"run_id":"r1","rec',
    "valid json without newline": serialize_record(sample_record(9)).rstrip(b"\n"),
    "invalid utf-8": b"\xff\xfe\x00 broken \xc3",
}


@pytest.mark.parametrize("name", sorted(FRAGMENTS))
def test_a_repair_interrupted_at_every_possible_byte_never_loses_a_byte_and_is_finished_by_the_next_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Cut the repair append after k bytes for every k, then repair again until the tail is clean."""
    fragment = FRAGMENTS[name]
    original = good_lines(2) + fragment
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    pw = EvidenceWriter(probe_dir, Durability.PROCESS)
    full_record = repair_file(put(probe_dir, original), pw, now)
    full_length = 2 + len(serialize_record(full_record))  # NUL marker, newline, record (D30)
    unrecorded = []
    for cut in range(0, full_length + 1):
        directory = tmp_path / f"cut{cut}"
        directory.mkdir()
        w = EvidenceWriter(directory, Durability.PROCESS)
        path = put(directory, original)
        with monkeypatch.context() as patch:
            spy_open(patch, [], limit=cut)
            with pytest.raises(OSError):
                repair_file(path, w, now)
        assert path.read_bytes().startswith(original)  # whatever was written, the original bytes are intact
        for _ in range(4):
            if repair_file(path, w, now) is None:
                break
        else:
            pytest.fail(f"cut {cut}: the tail was still not clean after four repairs")
        final = path.read_bytes()
        assert final.startswith(original) and final.endswith(b"\n")
        assert inspect_tail(path).last_line_valid_json
        # the damaged bytes are always still there, starting a line of their own, followed by one or two markers
        lines = final.split(b"\n")
        assert lines[2] in (fragment + b"\x00", fragment + b"\x00\x00")
        result = read_evidence(path)
        assert summarize(result).sample_count == 2 and [r["sample"]["seq"] for r in result.records if r["record_type"] == "sample"] == [0, 1]
        assert 3 in {e.line_no for e in result.errors}  # the fragment line is never accepted
        decoded = [decode(json.loads(x)) for x in lines[:-1] if x.startswith(b'{"record_type":"repair"') and x.endswith(b"}")]
        # a retry after a marker-only cut records the earlier marker too, so the original is a prefix
        if not any(d.startswith(fragment) and len(d) <= len(fragment) + 1 for d in decoded):
            unrecorded.append(cut)
    # D30: cut 1 (marker only) is recorded by the retry with the marker included; cut 2 (marker and newline) by a
    # none_needed_already_terminated retry. A cut of 3 bytes up to the byte before the record's final newline
    # leaves a partial or unterminated repair record as the tail; the next repair isolates that line instead
    # (and marks it), so the original fragment gets no repair record of its own. No byte is lost, and the
    # fragment line is never accepted as a record.
    assert unrecorded == list(range(3, full_length))

@pytest.mark.parametrize("cut", [1, 2], ids=["marker", "marker-nl"])
def test_a_valid_json_fragment_cut_after_the_marker_is_never_read_as_a_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: int) -> None:
    """D30 closes the former limitation: once the NUL marker persists, the never-acknowledged object is invalid JSON."""
    w, _, directory = make(tmp_path, Durability.PROCESS)
    fragment = serialize_record(sample_record(9)).rstrip(b"\n")
    path = put(directory, good_lines(1) + fragment)
    with monkeypatch.context() as patch:
        spy_open(patch, [], limit=cut)
        with pytest.raises(OSError):
            repair_file(path, w, now)
    assert path.read_bytes().endswith(fragment + b"\x00\n"[:cut])
    assert summarize(read_evidence(path)).sample_count == 1
    record = repair_file(path, w, now)  # not clean: the marker made the line invalid
    assert record is not None and decode(record) == fragment + b"\x00"
    assert record["action"] == ("appended_newline" if cut == 1 else "none_needed_already_terminated")
    assert repair_file(path, w, now) is None
    assert summarize(read_evidence(path)).sample_count == 1


def test_a_partial_repair_record_is_isolated_by_the_next_repair_and_every_byte_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w, _, directory = make(tmp_path, Durability.PROCESS)
    fragment = b'{"half'
    original = good_lines(1) + fragment
    path = put(directory, original)
    with monkeypatch.context() as patch:
        spy_open(patch, [], limit=40)
        with pytest.raises(OSError):
            repair_file(path, w, now)
    partial = path.read_bytes()[len(original) + 2 :]
    assert len(partial) == 38 and path.read_bytes().startswith(original + b"\x00\n")
    second = repair_file(path, w, now)
    assert second["reason"] == "incomplete_last_line" and decode(second) == partial and second["line_no"] == 3
    assert path.read_bytes().startswith(original + b"\x00\n" + partial + b"\x00\n")
    assert repair_file(path, w, now) is None


# --- only the repair module's own surface -----------------------------------------------


def test_repair_never_truncates_and_never_opens_a_file_for_writing_other_than_through_the_writer() -> None:
    import ast
    import pathlib

    source = pathlib.Path(repair_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in {"truncate", "unlink", "remove", "rename", "replace", "write_bytes", "write_text"}:
            pytest.fail(f"repair.py calls {ast.dump(node.func)}")
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            mode = node.args[1] if len(node.args) > 1 else None
            assert isinstance(mode, ast.Constant) and mode.value == "rb", "repair.py may only open files read-only"
    assert "append_bytes_durably" in source


def test_repair_module_is_standard_library_plus_evidence_and_common_only() -> None:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(repair_module.__file__).read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert {m for m in modules if m.split(".")[0] not in sys.stdlib_module_names} <= {
        "coroner.common.timeutil", "coroner.evidence.schema", "coroner.evidence.writer"
    }

def test_an_oversized_last_line_is_never_valid_even_when_its_first_mebibyte_parses_as_an_object(tmp_path: Path) -> None:
    """The first FRAGMENT_CAP bytes alone can be a valid object (trailing spaces); the line is over TAIL_LINE_CAP."""
    w, _, directory = make(tmp_path, Durability.PROCESS)
    line = b'{"a":1}' + b" " * TAIL_LINE_CAP + b"x"
    path = put(directory, good_lines(1) + line + b"\n")
    tail = inspect_tail(path)
    assert _is_json_like_prefix(tail.last_line) and tail.last_line_len == len(line) and not tail.last_line_valid_json
    record = repair_file(path, w, now)
    assert record["reason"] == "invalid_json_last_line" and record["fragment_truncated"] is True
    assert record["action"] == "none_needed_already_terminated"


def _is_json_like_prefix(data: bytes) -> bool:
    return isinstance(json.loads(data), dict)


# --- repair size correction (design D28): 1 MiB fragment copy, 2 MiB tail-validity limit ----------------------------

MIB = 1024 * 1024
OLD_BOUNDARY = 786_000  # a fragment about this long made the old record cross 1 MiB (base64 grows it by 4/3)


def _repaired(tmp_path: Path, fragment: bytes, level: Durability = Durability.PROCESS):
    w, _, directory = make(tmp_path, level)
    original = good_lines(1) + fragment
    path = put(directory, original)
    record = repair_file(path, w, now)
    return w, path, original, record


def test_the_worst_case_repair_record_is_below_the_two_mebibyte_limit() -> None:
    record = {
        "record_type": "repair", "schema_version": 1, "run_id": "r" * 64, "recv_time": NOW, "file": "r" * 64 + ".jsonl",
        "line_no": 10**12, "fragment_len": 10**12, "fragment_b64": base64.b64encode(b"\xff" * FRAGMENT_CAP).decode("ascii"),
        "fragment_truncated": True, "reason": "missing_trailing_newline", "action": "none_needed_already_terminated",
    }
    size = len(serialize_record(record))
    assert 1_398_104 < size < 1_398_104 + 1000 and size < TAIL_LINE_CAP
    assert FRAGMENT_CAP == 1_048_576 and TAIL_LINE_CAP == 2_097_152


@pytest.mark.parametrize("n", [OLD_BOUNDARY - 5000, OLD_BOUNDARY, OLD_BOUNDARY + 5000, FRAGMENT_CAP - 1, FRAGMENT_CAP, FRAGMENT_CAP + 1, FRAGMENT_CAP + 4096],
                         ids=["below", "at", "above", "cap-1", "cap", "cap+1", "cap+4k"])
def test_an_oversized_fragment_repair_converges_and_the_file_accepts_the_next_append(tmp_path: Path, n: int) -> None:
    fragment = b"q" * n
    w, path, original, record = _repaired(tmp_path, fragment)
    assert record["fragment_len"] == n and record["fragment_truncated"] is (n > FRAGMENT_CAP)
    assert decode(record) == fragment[:FRAGMENT_CAP] and validate_record(record) == []
    assert path.read_bytes().startswith(original + b"\x00\n")  # every original byte survives, unmoved
    assert hashlib.sha256(path.read_bytes()[: len(original)]).digest() == hashlib.sha256(original).digest()
    line_len = len(serialize_record(record)) - 1
    if n >= OLD_BOUNDARY + 5000:
        assert line_len > FRAGMENT_CAP  # the old rule would have called this record invalid
    tail = inspect_tail(path)
    assert tail.ends_with_newline and tail.last_line_valid_json and tail.last_line_len == line_len
    assert w._tail_problem(path) is None
    assert repair_file(path, w, now) is None  # idempotent
    before = path.read_bytes()
    w.append("r1", [sample_record(1)])
    after = path.read_bytes()
    assert after.startswith(before) and after[len(before):] == serialize_record(sample_record(1))
    assert before.endswith(b"\n")  # the Sample starts on a fresh line
    assert repair_file(path, w, now) is None


@pytest.mark.parametrize("extra", [1, 1000, 10 * 1024], ids=["1b", "1k", "10k"])
def test_valid_lines_between_one_and_two_mebibytes_are_clean_for_inspect_tail_and_the_writer(tmp_path: Path, extra: int) -> None:
    w, _, directory = make(tmp_path)
    line = b'{"k":"' + b"v" * (FRAGMENT_CAP + extra) + b'"}'
    assert FRAGMENT_CAP < len(line) < TAIL_LINE_CAP
    path = put(directory, good_lines(1) + line + b"\n")
    tail = inspect_tail(path)
    assert tail.last_line_valid_json and tail.last_line_len == len(line) and len(tail.last_line) == FRAGMENT_CAP
    assert w._tail_problem(path) is None and repair_file(path, w, now) is None
    w.append("r1", [sample_record(1)])


def test_a_real_repair_record_over_one_mebibyte_is_clean_for_inspect_tail_and_the_writer(tmp_path: Path) -> None:
    w, path, _, record = _repaired(tmp_path, b"q" * FRAGMENT_CAP)
    assert FRAGMENT_CAP < len(serialize_record(record)) < TAIL_LINE_CAP
    assert inspect_tail(path).last_line_valid_json and w._tail_problem(path) is None
    w.append("r1", [sample_record(1)])


@pytest.mark.parametrize("shape", ["object", "junk"])
def test_lines_over_two_mebibytes_are_invalid_for_inspect_tail_and_the_writer_alike(tmp_path: Path, shape: str) -> None:
    w, _, directory = make(tmp_path)
    line = b'{"k":"' + b"v" * TAIL_LINE_CAP + b'"}' if shape == "object" else b"z" * (TAIL_LINE_CAP + 1)
    path = put(directory, good_lines(1) + line + b"\n")
    tail = inspect_tail(path)
    assert not tail.last_line_valid_json and tail.last_line_len == len(line)
    assert w._tail_problem(path) == "invalid_last_line"
    with pytest.raises(TailRepairRequired):
        w.append("r1", [sample_record(1)])
    record = repair_file(path, w, now)
    assert record["fragment_truncated"] is True and record["fragment_len"] == len(line)
    assert decode(record) == line[:FRAGMENT_CAP]
    assert repair_file(path, w, now) is None and w._tail_problem(path) is None


@pytest.mark.parametrize("which", ["one-byte", "middle", "before-nl"])
def test_an_interrupted_oversized_repair_converges_and_loses_no_byte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    fragment = b"q" * (FRAGMENT_CAP + 10)
    _, _, _, full = _repaired(tmp_path / "ref", fragment) if (tmp_path / "ref").mkdir() is None else (None,) * 4
    total = 2 + len(serialize_record(full))  # NUL marker, isolating newline, the record and its newline
    cut = {"one-byte": 1, "middle": total // 2, "before-nl": total - 1}[which]
    w, _, directory = make(tmp_path)
    original = good_lines(1) + fragment
    path = put(directory, original)
    with monkeypatch.context() as patch:
        spy_open(patch, [], limit=cut)
        with pytest.raises(OSError):
            repair_file(path, w, now)
    assert path.read_bytes() == original + (b"\x00\n" + serialize_record(full))[:cut]
    for _ in range(5):
        if repair_file(path, w, now) is None:
            break
    else:
        pytest.fail("repair did not converge")
    data = path.read_bytes()
    assert data.startswith(original) and data.endswith(b"\n")
    assert inspect_tail(path).last_line_valid_json and w._tail_problem(path) is None
    w.append("r1", [sample_record(1)])


def test_the_reader_quarantines_an_oversized_fragment_and_accepts_the_large_repair_record(tmp_path: Path) -> None:
    from coroner.evidence.reader import read_evidence

    w, path, _, record = _repaired(tmp_path, b"q" * (FRAGMENT_CAP + 10))
    w.append("r1", [sample_record(1)])
    result = read_evidence(path)
    assert [(e.line_no, e.kind, e.has_repair_record) for e in result.errors] == [(2, "isolated_by_repair", True)]
    assert [r["record_type"] for r in result.records] == ["sample", "repair", "sample"]
    assert result.records[1]["fragment_truncated"] is True


# --- fragments longer than TAIL_LINE_CAP (2 MiB) ----------------------------------------------------------------------

BIG = TAIL_LINE_CAP + 12345


@pytest.mark.parametrize("terminated", [False, True], ids=["unterminated", "terminated"])
def test_a_fragment_over_two_mebibytes_is_repaired_quarantined_and_converges(tmp_path: Path, terminated: bool) -> None:
    from coroner.evidence.reader import read_evidence

    fragment = bytes(range(33, 127)) * (BIG // 94 + 1)
    fragment = fragment[:BIG]  # printable, not JSON
    assert len(fragment) == BIG and BIG > TAIL_LINE_CAP
    tail = fragment + (b"\n" if terminated else b"")
    w, _, directory = make(tmp_path, Durability.PROCESS)
    original = good_lines(1) + tail
    path = put(directory, original)
    record = repair_file(path, w, now)
    assert record["fragment_len"] == BIG and record["fragment_truncated"] is True
    assert base64.b64decode(record["fragment_b64"]) == fragment[:1_048_576]
    assert record["action"] == ("none_needed_already_terminated" if terminated else "appended_newline")
    data = path.read_bytes()
    expected_prefix = original if terminated else original + b"\x00\n"
    assert data.startswith(expected_prefix)
    assert hashlib.sha256(data[: len(expected_prefix)]).digest() == hashlib.sha256(expected_prefix).digest()
    assert repair_file(path, w, now) is None  # converges
    before = path.read_bytes()
    w.append("r1", [sample_record(1)])
    after = path.read_bytes()
    assert before.endswith(b"\n") and after[len(before):] == serialize_record(sample_record(1))
    result = read_evidence(path)
    assert [r["record_type"] for r in result.records] == ["sample", "repair", "sample"]
    assert result.line_numbers == [1, 3, 4]
    # the physical line is the fragment plus the D30 marker when the repair appended a newline
    assert len(result.errors) == 1 and result.errors[0].line_no == 2
    assert result.errors[0].byte_length == BIG + (0 if terminated else 1)
    assert result.errors[0].has_repair_record is True
    if not terminated:
        assert result.errors[0].kind == "isolated_by_repair"
    else:
        assert result.errors[0].kind == "invalid_json"
