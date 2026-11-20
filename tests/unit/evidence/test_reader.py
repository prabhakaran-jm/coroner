"""Tests for coroner.evidence.reader.read_evidence (task 2.11; requirements 5.9 to 5.11; design D19, D26, D27).

`summarize` is task 2.12; Properties 1 and 2 and the repair/reader example file are tasks 2.13 and 2.14.
Everything here is a deterministic example. Files live under `tmp_path`.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from coroner.evidence import reader as reader_module
from coroner.evidence.reader import ReadError, ReadResult, read_evidence
from coroner.evidence.repair import repair_file
from coroner.evidence.writer import Durability, EvidenceWriter, serialize_record

RECV = "2025-01-01T12:00:06.530Z"
NOW = "2025-01-01T12:05:00.004Z"


def sample_record(seq: int = 0, run_id: str = "r1") -> dict:
    sample = {
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 71303168, "request_count": 9,
        "loop_delay_ms": 0.84, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }
    return {"record_type": "sample", "schema_version": 1, "run_id": run_id, "recv_time": RECV, "sample": sample}


def repair_record(line_no: int, action: str = "appended_newline", run_id: str | None = "r1", file: str = "r1.jsonl") -> dict:
    return {
        "record_type": "repair", "schema_version": 1, "run_id": run_id, "recv_time": NOW, "file": file, "line_no": line_no,
        "fragment_len": 3, "fragment_b64": "eHh4", "fragment_truncated": False,
        "reason": "incomplete_last_line" if action == "appended_newline" else "invalid_json_last_line", "action": action,
    }


def rejection_record() -> dict:
    return {"record_type": "rejection", "schema_version": 1, "run_id": None, "recv_time": RECV, "http_status": 422, "reason_code": "invalid_json",
            "field": "$", "reason": "body is not JSON", "body_len": 3, "body_preview_b64": "eHh4"}


def lines(*records: dict) -> bytes:
    return b"".join(serialize_record(r) for r in records)


def read(data: bytes, name: str = "r1.jsonl") -> ReadResult:
    return read_evidence(data, name=name)


def kinds(result: ReadResult) -> list[tuple[int, str]]:
    return [(e.line_no, e.kind) for e in result.errors]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_writer(tmp_path: Path) -> tuple[EvidenceWriter, Path]:
    directory = tmp_path / "evidence"
    directory.mkdir()
    return EvidenceWriter(directory, Durability.PROCESS), directory


# --- basic shapes -------------------------------------------------------------------------


def test_an_empty_file_has_no_records_and_no_errors() -> None:
    result = read(b"")
    assert (result.records, result.line_numbers, result.errors, result.path) == ([], [], [], "r1.jsonl")


def test_valid_records_are_returned_in_order_with_their_line_numbers() -> None:
    result = read(lines(sample_record(0), sample_record(1), sample_record(2)))
    assert [r["sample"]["seq"] for r in result.records] == [0, 1, 2]
    assert result.line_numbers == [1, 2, 3] and result.errors == []


def test_a_final_newline_does_not_create_an_empty_last_line() -> None:
    assert len(read(lines(sample_record())).records) == 1
    assert kinds(read(lines(sample_record()) + b"\n")) == [(2, "invalid_json")]  # a real blank line is malformed


def test_a_blank_line_in_the_middle_is_quarantined_and_numbers_stay_true() -> None:
    result = read(lines(sample_record(0)) + b"\n" + lines(sample_record(1)))
    assert kinds(result) == [(2, "invalid_json")] and result.errors[0].byte_length == 0
    assert result.line_numbers == [1, 3]


def test_the_file_and_the_record_objects_come_from_the_parsed_lines() -> None:
    record = sample_record(3)
    assert read(lines(record)).records == [record]


# --- malformed lines ----------------------------------------------------------------------


def test_invalid_utf8_is_quarantined_with_its_byte_length() -> None:
    bad = b'{"a":"\xff\xfe"}'
    result = read(lines(sample_record(0)) + bad + b"\n" + lines(sample_record(1)))
    assert kinds(result) == [(2, "invalid_utf8")]
    error = result.errors[0]
    assert error.byte_length == len(bad) and error.file == "r1.jsonl" and error.has_repair_record is False
    assert result.line_numbers == [1, 3]


@pytest.mark.parametrize("bad", [b"garbage", b'{"a":', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}', b"\xef\xbb\xbf{}", b"{'a':1}", b'{"a":1}{"b":2}'],
                         ids=["word", "cut", "nan", "inf", "ninf", "bom", "quotes", "two"])
def test_text_that_is_not_json_is_invalid_json(bad: bytes) -> None:
    assert kinds(read(bad + b"\n")) == [(1, "invalid_json")]


def test_deeply_nested_json_is_invalid_json_not_a_crash() -> None:
    assert kinds(read(b"[" * 100000 + b"\n")) == [(1, "invalid_json")]


@pytest.mark.parametrize("bad", [b"[1,2]", b"5", b'"text"', b"null", b"true"], ids=["array", "number", "string", "null", "bool"])
def test_json_that_is_not_an_object_is_not_an_object(bad: bytes) -> None:
    assert kinds(read(bad + b"\n")) == [(1, "not_an_object")]


def test_an_unterminated_last_line_is_incomplete_last_line_even_if_it_parses() -> None:
    complete = lines(sample_record(0))
    result = read(complete + complete.rstrip(b"\n"))
    assert kinds(result) == [(2, "incomplete_last_line")] and result.line_numbers == [1]
    assert result.errors[0].byte_length == len(complete) - 1
    assert kinds(read(lines(sample_record(0)) + b'{"half')) == [(2, "incomplete_last_line")]
    assert kinds(read(b"x")) == [(1, "incomplete_last_line")]


@pytest.mark.parametrize(
    "record",
    [
        {"record_type": "mystery", "schema_version": 1, "run_id": "r1", "recv_time": RECV},
        {"record_type": "gap", "schema_version": 2, "run_id": "r1", "recv_time": RECV},
        {"record_type": "gap", "schema_version": 1, "run_id": "bad id", "recv_time": RECV},
        {"record_type": "gap", "schema_version": 1, "run_id": "r1"},
        {"record_type": "gap", "schema_version": 1, "run_id": "r1", "recv_time": "yesterday"},
        {"hello": "world"},
        {},
    ],
    ids=["type", "version", "runid", "recv", "time", "foreign", "empty"],
)
def test_an_object_that_is_not_a_valid_record_is_invalid_record(record: dict) -> None:
    result = read(json.dumps(record).encode() + b"\n")
    assert kinds(result) == [(1, "invalid_record")] and "not a valid record" in result.errors[0].detail


def test_a_sample_with_a_bad_nested_field_is_invalid_record() -> None:
    record = sample_record()
    record["sample"]["seq"] = -1
    assert kinds(read(lines(record))) == [(1, "invalid_record")]


def test_every_error_kind_is_one_of_the_designs_six() -> None:
    data = b'\xff\n{"a":\n[]\n{}\n' + lines(sample_record()) + b"tail"
    result = read(data)
    assert {e.kind for e in result.errors} <= set(reader_module.READ_ERROR_KINDS)
    assert [e.kind for e in result.errors] == ["invalid_utf8", "invalid_json", "not_an_object", "invalid_record", "incomplete_last_line"]
    assert reader_module.READ_ERROR_KINDS == (
        "invalid_utf8", "invalid_json", "not_an_object", "incomplete_last_line", "invalid_record", "isolated_by_repair"
    )


def test_a_good_line_after_many_bad_ones_is_still_read() -> None:
    result = read(b"x\n" * 50 + lines(sample_record()))
    assert len(result.errors) == 50 and result.line_numbers == [51]


# --- file context (D27) -------------------------------------------------------------------


def test_a_record_for_another_run_is_invalid_in_a_run_file() -> None:
    result = read(lines(sample_record(0, "r2")), name="r1.jsonl")
    assert kinds(result) == [(1, "invalid_record")] and "run of" not in result.errors[0].detail
    assert read(lines(sample_record(0, "r2")), name="r2.jsonl").errors == []


def test_a_null_run_id_repair_record_is_valid_only_in_the_rejections_file() -> None:
    data = b"x\n" + lines(repair_record(1, run_id=None, file="_rejections.jsonl"))
    in_rejections = read(data, name="_rejections.jsonl")
    assert in_rejections.line_numbers == [2] and in_rejections.records[0]["run_id"] is None
    in_run = read(data, name="r1.jsonl")
    assert kinds(in_run) == [(1, "invalid_json"), (2, "invalid_record")]
    assert "null run_id" not in in_run.errors[1].detail and "does not belong" in in_run.errors[1].detail


def test_a_null_run_id_rejection_record_is_invalid_in_a_run_file_and_valid_in_the_rejections_file() -> None:
    assert read(lines(rejection_record()), name="_rejections.jsonl").line_numbers == [1]
    assert kinds(read(lines(rejection_record()), name="r1.jsonl")) == [(1, "invalid_record")]


@pytest.mark.parametrize(
    "record",
    [sample_record(0, "r1"), repair_record(1, run_id="r1"), {"record_type": "gap", "schema_version": 1, "run_id": "r1", "recv_time": RECV},
     {**rejection_record(), "run_id": "r1"}],
    ids=["sample", "repair_with_run", "gap", "rejection_with_run"],
)
def test_the_rejections_file_holds_only_null_run_id_rejection_and_repair_records(record: dict) -> None:
    assert kinds(read(lines(record), name="_rejections.jsonl")) == [(1, "invalid_record")]


def test_an_unnamed_source_accepts_a_null_rejection_but_not_a_null_repair_and_does_not_compare_run_ids() -> None:
    assert read_evidence(lines(rejection_record())).errors == []
    assert [e.kind for e in read_evidence(b"x\n" + lines(repair_record(1, run_id=None))).errors] == ["invalid_json", "invalid_record"]
    result = read_evidence(lines(sample_record(0, "a"), sample_record(0, "b")))
    assert result.errors == [] and result.path == "<bytes>"
    assert read_evidence(b"zz\n").errors[0].file == "<bytes>"


def test_a_file_name_that_is_not_an_evidence_name_gives_no_context() -> None:
    assert read(lines(sample_record(0, "other")), name="notes.txt").errors == []
    assert read(lines(sample_record(0, "other")), name="bad name.jsonl").errors == []


# --- repair linkage (D19) -----------------------------------------------------------------


def test_a_fragment_that_is_a_valid_sample_is_isolated_by_repair_and_not_counted() -> None:
    fragment = lines(sample_record(1)).rstrip(b"\n")  # complete JSON, but it never got its newline
    data = lines(sample_record(0)) + fragment + b"\n" + lines(repair_record(2)) + lines(sample_record(2))
    result = read(data)
    assert kinds(result) == [(2, "isolated_by_repair")]
    error = result.errors[0]
    assert error.has_repair_record is True and error.byte_length == len(fragment)
    assert [r["record_type"] for r in result.records] == ["sample", "repair", "sample"]
    assert result.line_numbers == [1, 3, 4]
    assert [r["sample"]["seq"] for r in result.records if r["record_type"] == "sample"] == [0, 2]  # the isolated seq 1 is gone


def test_an_isolated_fragment_that_is_also_broken_is_isolated_by_repair_with_the_cause_in_the_detail() -> None:
    data = b'{"half\n' + lines(repair_record(1, action="appended_newline"))
    error = read(data).errors[0]
    assert (error.line_no, error.kind, error.has_repair_record) == (1, "isolated_by_repair", True)
    assert "invalid_json" in error.detail


def test_a_none_needed_repair_links_the_line_but_keeps_its_own_kind() -> None:
    data = b"garbage\n" + lines(repair_record(1, action="none_needed_already_terminated"))
    result = read(data)
    assert kinds(result) == [(1, "invalid_json")] and result.errors[0].has_repair_record is True
    assert result.line_numbers == [2]


def test_a_valid_line_named_by_a_none_needed_repair_stays_valid() -> None:
    data = lines(sample_record(0)) + lines(repair_record(1, action="none_needed_already_terminated"))
    assert read(data).errors == []


def test_a_bad_line_without_a_repair_record_has_has_repair_record_false() -> None:
    assert read(b"garbage\n").errors[0].has_repair_record is False


@pytest.mark.parametrize(
    "overrides",
    [{"line_no": 2}, {"line_no": 3}, {"line_no": 0}, {"line_no": -1}, {"line_no": True}, {"line_no": "1"}, {"line_no": 1.0}, {"action": 5},
     {"file": "other.jsonl"}, {"line_no": None}],
    ids=["self", "later", "zero", "neg", "bool", "str", "float", "action", "file", "none"],
)
def test_a_repair_record_that_does_not_name_a_preceding_line_in_this_file_links_nothing(overrides: dict) -> None:
    record = {**repair_record(1), **overrides}
    result = read(b"garbage\n" + lines(record))
    assert result.errors[0].kind == "invalid_json" and result.errors[0].has_repair_record is False


def test_a_repair_record_that_does_not_validate_links_nothing() -> None:
    record = {**repair_record(1), "schema_version": 9}
    result = read(b"garbage\n" + lines(record))
    assert [e.has_repair_record for e in result.errors] == [False, False]


def test_a_repair_record_before_the_line_it_names_does_not_link_it() -> None:
    data = lines(repair_record(2)) + lines(sample_record(0))
    assert read(data).errors == []


def test_a_repair_record_for_another_run_or_a_null_run_in_a_run_file_links_nothing() -> None:
    for record in (repair_record(1, run_id="r2"), repair_record(1, run_id=None)):
        result = read(b'{"half\n' + lines(record))
        assert kinds(result) == [(1, "invalid_json"), (2, "invalid_record")] and result.errors[0].has_repair_record is False


def test_a_repair_record_without_a_file_field_is_invalid_since_task_2_19_and_links_nothing() -> None:
    record = repair_record(1)
    del record["file"]
    result = read(b'{"half\n' + lines(record))
    assert kinds(result) == [(1, "invalid_json"), (2, "invalid_record")] and result.errors[0].has_repair_record is False


def test_the_file_field_is_not_compared_when_the_source_has_no_name() -> None:
    data = b'{"half\n' + lines(repair_record(1, run_id="whatever", file="whatever.jsonl"))
    assert read_evidence(data).errors[0].kind == "isolated_by_repair"


def test_two_repairs_name_two_lines_and_a_line_named_twice_is_isolated_once() -> None:
    data = b"aa\nbb\n" + lines(repair_record(1), repair_record(2), repair_record(1))
    result = read(data)
    assert kinds(result) == [(1, "isolated_by_repair"), (2, "isolated_by_repair")] and result.line_numbers == [3, 4, 5]


def test_a_quarantined_repair_record_still_links_its_fragment() -> None:
    """Linking can only quarantine more, never less, so a repair line that is itself isolated still counts."""
    first = lines(repair_record(1)).rstrip(b"\n")  # complete record, no newline: it was cut before its own newline
    data = b'{"half\n' + first + b"\n" + lines(repair_record(2))
    assert kinds(read(data)) == [(1, "isolated_by_repair"), (2, "isolated_by_repair")]


# --- real repairs, written by the library -------------------------------------------------


def clock() -> str:
    return NOW


def test_a_file_repaired_by_repair_file_reads_back_with_the_fragment_quarantined(tmp_path: Path) -> None:
    w, directory = make_writer(tmp_path)
    path = directory / "r1.jsonl"
    w.append("r1", [sample_record(0)])
    fragment = serialize_record(sample_record(1)).rstrip(b"\n")  # an unacknowledged, complete-looking line
    with open(path, "ab") as handle:
        handle.write(fragment)
    repair_file(path, w, clock)
    w.append("r1", [sample_record(2)])
    result = read_evidence(path)
    # the physical line is the fragment plus the one-byte D30 marker
    assert kinds(result) == [(2, "isolated_by_repair")] and result.errors[0].byte_length == len(fragment) + 1
    assert [r["record_type"] for r in result.records] == ["sample", "repair", "sample"]
    assert [r["sample"]["seq"] for r in result.records if r["record_type"] == "sample"] == [0, 2]
    assert fragment in path.read_bytes()  # the bytes are still in the file


def test_a_terminated_invalid_tail_repaired_by_repair_file_keeps_its_kind_and_links(tmp_path: Path) -> None:
    w, directory = make_writer(tmp_path)
    path = directory / "r1.jsonl"
    path.write_bytes(lines(sample_record(0)) + b"garbage\n")
    repair_file(path, w, clock)
    result = read_evidence(path)
    assert [(e.line_no, e.kind, e.has_repair_record) for e in result.errors] == [(2, "invalid_json", True)]


def test_the_rejections_file_repaired_by_repair_file_reads_clean(tmp_path: Path) -> None:
    w, directory = make_writer(tmp_path)
    path = directory / "_rejections.jsonl"
    path.write_bytes(lines(rejection_record()) + b'{"half')
    repair_file(path, w, clock)
    result = read_evidence(path)
    assert kinds(result) == [(2, "isolated_by_repair")] and result.errors[0].file == "_rejections.jsonl"
    assert [r["record_type"] for r in result.records] == ["rejection", "repair"] and result.records[1]["run_id"] is None


# --- interrupted repairs ------------------------------------------------------------------


def test_a_repair_interrupted_after_the_newline_with_a_valid_sample_fragment_is_a_documented_limit() -> None:
    """D27 limitation: only the isolating newline reached the file. Nothing marks the fragment as isolated, so a
    fragment that is itself a complete valid record is read as valid. The reader invents no audit record.
    Since D30 `repair_file` writes a NUL marker before the newline, so it no longer produces this file; the
    case stays as a reader example (e.g. a file repaired before D30)."""
    fragment = lines(sample_record(1)).rstrip(b"\n")
    result = read(lines(sample_record(0)) + fragment + b"\n")
    assert result.errors == [] and result.line_numbers == [1, 2]


def test_a_repair_interrupted_after_the_newline_with_a_broken_fragment_is_an_ordinary_invalid_line() -> None:
    result = read(lines(sample_record(0)) + b'{"half\n')
    assert [(e.line_no, e.kind, e.has_repair_record) for e in result.errors] == [(2, "invalid_json", False)]


def test_a_partial_repair_record_as_the_tail_is_incomplete_last_line_then_isolated_by_the_next_repair(tmp_path: Path) -> None:
    w, directory = make_writer(tmp_path)
    path = directory / "r1.jsonl"
    full = lines(repair_record(2)).rstrip(b"\n")
    path.write_bytes(lines(sample_record(0)) + b'{"half' + b"\n" + full[:40])  # newline and half of the repair record
    assert kinds(read_evidence(path)) == [(2, "invalid_json"), (3, "incomplete_last_line")]
    repair_file(path, w, clock)  # the second run repairs the partial record
    result = read_evidence(path)
    assert kinds(result) == [(2, "invalid_json"), (3, "isolated_by_repair")]
    assert result.errors[1].has_repair_record is True and result.errors[0].has_repair_record is False
    assert result.line_numbers == [1, 4]


def test_a_complete_repair_record_cut_before_its_newline_is_isolated_by_the_next_repair(tmp_path: Path) -> None:
    w, directory = make_writer(tmp_path)
    path = directory / "r1.jsonl"
    path.write_bytes(lines(sample_record(0)) + b'{"half\n' + lines(repair_record(2)).rstrip(b"\n"))
    # the complete but unterminated repair record already links line 2 (its newline did get written)
    assert kinds(read_evidence(path)) == [(2, "isolated_by_repair"), (3, "incomplete_last_line")]
    repair_file(path, w, clock)
    result = read_evidence(path)
    # D30: the new repair puts its NUL marker after the unterminated record, so line 3 is no longer a valid repair
    # record and no longer links line 2; line 2 is an ordinary invalid line, line 3 is isolated by the new repair
    assert [(e.line_no, e.kind, e.has_repair_record) for e in result.errors] == [
        (2, "invalid_json", False), (3, "isolated_by_repair", True)]
    assert result.line_numbers == [1, 4]


# --- inputs and invariants ----------------------------------------------------------------


def test_path_and_bytes_give_the_same_result(tmp_path: Path) -> None:
    data = lines(sample_record(0)) + b"garbage\n" + lines(sample_record(1)) + b'{"half'
    path = tmp_path / "r1.jsonl"
    path.write_bytes(data)
    from_path, from_bytes = read_evidence(path), read_evidence(data, name="r1.jsonl")
    assert (from_path.records, from_path.line_numbers, from_path.errors) == (from_bytes.records, from_bytes.line_numbers, from_bytes.errors)
    assert from_path.path == str(path) and from_bytes.path == "r1.jsonl"


def test_reading_does_not_modify_the_file_and_is_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "r1.jsonl"
    path.write_bytes(lines(sample_record(0)) + b"\xff\n" + lines(repair_record(2)) + b'{"half')
    before, mtime = sha(path), path.stat().st_mtime_ns
    first, second = read_evidence(path), read_evidence(path)
    assert sha(path) == before and path.stat().st_mtime_ns == mtime
    assert first == second


def test_the_input_bytes_object_and_a_bytearray_are_accepted_and_unchanged() -> None:
    data = lines(sample_record(0))
    assert read_evidence(bytearray(data), name="r1.jsonl").line_numbers == [1]
    assert read_evidence(memoryview(data), name="r1.jsonl").line_numbers == [1]
    assert data == lines(sample_record(0))


def test_windows_line_endings_are_not_translated() -> None:
    data = lines(sample_record(0)).replace(b"\n", b"\r\n")
    result = read(data)
    assert result.line_numbers == [1] and result.errors == []  # JSON allows the trailing carriage return
    assert read(b"x\r\n").errors[0].byte_length == 2  # the carriage return is part of the line


def test_non_ascii_and_line_separators_inside_a_record_do_not_split_it() -> None:
    record = sample_record(0)
    record["sample"]["container_id"] = None
    record["sample"]["null_reasons"] = {"container_id": "caf\u00e9 \u2028 \u2029 \x0b \x0c"}
    raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    result = read(raw)
    assert result.errors == [] and result.records == [record]


def test_name_overrides_the_path_name(tmp_path: Path) -> None:
    path = tmp_path / "anything.dat"
    path.write_bytes(lines(sample_record(0, "r9")))
    assert read_evidence(path).errors == []  # no context from `anything.dat`
    assert [e.kind for e in read_evidence(path, name="r1.jsonl").errors] == ["invalid_record"]
    assert read_evidence(path, name="r1.jsonl").errors[0].file == "r1.jsonl"


def test_a_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_evidence(tmp_path / "nope.jsonl")


@pytest.mark.parametrize("source", ["r1.jsonl", 5, None, [b"x"]], ids=["str", "int", "none", "list"])
def test_other_source_types_are_refused(source: object) -> None:
    with pytest.raises(TypeError):
        read_evidence(source)  # type: ignore[arg-type]


def test_read_error_and_result_have_the_designed_fields() -> None:
    assert list(ReadError.__dataclass_fields__) == ["file", "line_no", "byte_length", "kind", "detail", "has_repair_record"]
    assert list(ReadResult.__dataclass_fields__) == ["path", "records", "line_numbers", "errors"]
    with pytest.raises(AttributeError):
        ReadError("f", 1, 0, "invalid_json", "", False).line_no = 2  # type: ignore[misc]


def test_records_and_line_numbers_are_parallel_and_errors_are_in_line_order() -> None:
    data = b"x\n" + lines(sample_record(0)) + b"[]\n" + lines(sample_record(1)) + b"tail"
    result = read(data)
    assert len(result.records) == len(result.line_numbers) == 2
    assert [e.line_no for e in result.errors] == sorted(e.line_no for e in result.errors)
    assert sorted(result.line_numbers + [e.line_no for e in result.errors]) == [1, 2, 3, 4, 5]


def test_a_large_file_reads_quickly_and_keeps_numbers() -> None:
    data = lines(*[sample_record(i) for i in range(2000)]) + b"bad\n" + lines(sample_record(2000))
    result = read(data)
    assert len(result.records) == 2001 and result.errors[0].line_no == 2001 and result.line_numbers[-1] == 2002


def test_the_module_never_opens_a_file_for_writing_and_imports_only_allowed_packages() -> None:
    tree = ast.parse(Path(reader_module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else None
            assert mode == "rb"
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("coroner"):
            assert node.module.startswith(("coroner.evidence", "coroner.common"))
