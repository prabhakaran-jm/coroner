"""Tests for coroner.evidence.writer (tasks 2.4 and 2.5; requirements 4.6, 4.10 to 4.15, 5.4, 5.12).

Everything runs in pytest's temporary directories with recording doubles for `fsync` and the
directory fsync, so no host crash, Docker, or network is needed. Property 6 (durability
configuration parsing) is in test_prop_durability.py. The last section was added by task 2.5
after a coverage audit.
"""
from __future__ import annotations

import builtins
import json
import os
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from coroner.common import envparse
from coroner.evidence import writer as writer_module
from coroner.evidence.writer import (
    AppendResult,
    ConfigError,
    Durability,
    EvidenceWriter,
    RecordError,
    TailRepairRequired,
    fsync_directory,
    parse_durability,
    serialize_record,
)

RECV = "2025-01-01T12:00:06.530Z"


def sample_obj(run_id: str = "r1", seq: int = 0) -> dict:
    return {
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 71303168, "request_count": 9,
        "loop_delay_ms": 0.84, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }


def sample_record(run_id: str = "r1", seq: int = 0) -> dict:
    return {"record_type": "sample", "schema_version": 1, "run_id": run_id, "recv_time": RECV, "sample": sample_obj(run_id, seq)}


def envelope(record_type: str, run_id: str | None = "r1", **extra: object) -> dict:
    return {"record_type": record_type, "schema_version": 1, "run_id": run_id, "recv_time": RECV, **extra}


def meta_record(run_id: str = "r1") -> dict:
    return envelope("run_metadata", run_id, event="created", durability="crash", fsync_before_ack=True, evidence_file=f"{run_id}.jsonl", collector_version="0.1.0")


def silence_gap(run_id: str = "r1") -> dict:
    return envelope(
        "gap", run_id, gap_id="gap-0001", gap_type="silence_timeout", start_time=RECV, end_time=RECV, last_sample_time=RECV, detected_time=RECV,
        threshold_ms=1500, interval_ms=500, sample_count="unknown", reconciliation="not_applicable", reconciles_with=[],
    )


def termination_record(run_id: str = "r1", note: str = "Observed after the fact.") -> dict:
    return envelope(
        "termination", run_id, source="experiment_runner", observed_time=RECV, container_state={"status": "exited", "exit_code": 137, "oom_killed": True},
        cause_label="unknown", note=note,
    )


def rejection_record(run_id: str | None = None) -> dict:
    return envelope(
        "rejection", run_id, http_status=422, reason_code="invalid_json", field="$", reason="body is not JSON", body_len=3, body_preview_b64="eHh4"
    )


def repair_rec(run_id: str | None = None, file: str = "_rejections.jsonl") -> dict:
    return envelope(
        "repair", run_id, file=file, line_no=1, fragment_len=3, fragment_b64="eHh4", fragment_truncated=False, reason="incomplete_last_line",
        action="appended_newline",
    )


class Recorder:
    """Records `fsync` and directory-fsync calls, and how much of the file was on disk at each fsync."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def fsync(self, fd: int) -> None:
        self.events.append(("fsync_file", os.fstat(fd).st_size))

    def fsync_dir(self, path: Path) -> None:
        self.events.append(("fsync_dir", Path(path)))

    @property
    def kinds(self) -> list[str]:
        return [event[0] for event in self.events]


def make(tmp_path: Path, level: Durability = Durability.CRASH) -> tuple[EvidenceWriter, Recorder, Path]:
    directory = tmp_path / "evidence"
    directory.mkdir()
    recorder = Recorder()
    return EvidenceWriter(directory, level, fsync=recorder.fsync, fsync_dir=recorder.fsync_dir), recorder, directory


def lines_of(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


# --- ConfigError and parse_durability ----------------------------------------------


def test_config_error_is_the_one_defined_in_common_envparse() -> None:
    assert ConfigError is envparse.ConfigError
    assert ConfigError.__module__ == "coroner.common.envparse"


def test_durability_values() -> None:
    assert [d.value for d in Durability] == ["process", "crash"]
    assert Durability("crash") is Durability.CRASH


@pytest.mark.parametrize("raw, expected", [(None, Durability.CRASH), ("", Durability.CRASH), ("crash", Durability.CRASH), ("process", Durability.PROCESS)])
def test_parse_durability_valid(raw: str | None, expected: Durability) -> None:
    assert parse_durability(raw) is expected


@pytest.mark.parametrize("raw", ["Crash", "CRASH", "Process", " process", "process ", "crash\n", " ", "none", "0", "1", "true", "fsync", "crash,process", "proces"])
def test_parse_durability_invalid_names_the_value(raw: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_durability(raw)
    assert str(info.value) == f"invalid CORONER_DURABILITY={raw!r} (expected 'process' or 'crash')"


# --- serialize_record --------------------------------------------------------------


def test_serialize_is_compact_one_line_ending_in_one_newline() -> None:
    data = serialize_record({"b": 1, "a": [1, 2, {"c": None}], "d": "x y"})
    assert data == b'{"b":1,"a":[1,2,{"c":null}],"d":"x y"}\n'
    assert data.count(b"\n") == 1 and data.endswith(b"\n")


def test_serialize_preserves_key_order() -> None:
    assert serialize_record({"z": 1, "a": 2}) == b'{"z":1,"a":2}\n'


TRICKY_STRINGS = [
    "", "plain", "line1\nline2", "cr\rlf\r\n", "tab\there", 'quote " and backslash \\', "é ü ñ 日本語", "emoji 😀",
    "\u2028 line sep \u2029 para sep", "nul \x00 char", "\x85 next line", "\ud800 lone surrogate", "x" * 100000,
]


@pytest.mark.parametrize("text", TRICKY_STRINGS, ids=[f"string{i}" for i in range(len(TRICKY_STRINGS))])
def test_serialize_round_trips_tricky_strings_on_exactly_one_line(text: str) -> None:
    record = {"record_type": "x", "text": text, "nested": {"list": [text]}}
    data = serialize_record(record)
    assert data.count(b"\n") == 1 and data.endswith(b"\n")
    assert b"\r" not in data
    data.decode("ascii")  # pure ASCII, therefore valid UTF-8
    assert len(data.splitlines()) == 1
    assert json.loads(data) == record


def test_serialize_round_trips_numbers_booleans_and_null() -> None:
    record = {"i": 0, "big": 2**80, "neg": -5, "f": 0.1, "tiny": 1e-300, "huge": 1e300, "t": True, "f2": False, "n": None, "z": -0.0}
    again = json.loads(serialize_record(record))
    assert again == record
    assert again["t"] is True and again["f2"] is False and again["n"] is None
    assert isinstance(again["i"], int) and isinstance(again["f"], float)


def test_serialize_does_not_modify_the_record() -> None:
    import copy

    record = {"a": [1, {"b": "é"}], "c": {"d": None}}
    snapshot = copy.deepcopy(record)
    serialize_record(record)
    assert record == snapshot


def test_serialize_accepts_a_read_only_mapping_at_the_top_level() -> None:
    assert serialize_record(MappingProxyType({"a": 1})) == b'{"a":1}\n'


@pytest.mark.parametrize("value", [None, [], [{"a": 1}], "text", b"bytes", 5, 1.5, True, {1, 2}])
def test_serialize_rejects_things_that_are_not_mappings(value: object) -> None:
    with pytest.raises(RecordError):
        serialize_record(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_serialize_rejects_non_finite_numbers_anywhere(bad: float) -> None:
    for record in ({"x": bad}, {"a": {"b": [1, bad]}}):
        with pytest.raises(RecordError):
            serialize_record(record)


@pytest.mark.parametrize(
    "record",
    [{1: "a"}, {None: "a"}, {True: "a"}, {1.5: "a"}, {(1, 2): "a"}, {"a": {2: "b"}}, {"a": [{"b": {3: 1}}]}, {"a": ({4: 1},)}],
)
def test_serialize_rejects_non_string_keys_instead_of_converting_them(record: dict) -> None:
    with pytest.raises(RecordError, match="non-string key|cannot be serialized"):
        serialize_record(record)


@pytest.mark.parametrize("value", [{1, 2}, b"bytes", object(), lambda: None, {"a": MappingProxyType({"b": 1})}, complex(1, 2)])
def test_serialize_rejects_values_json_cannot_hold(value: object) -> None:
    with pytest.raises(RecordError, match="cannot be serialized"):
        serialize_record({"x": value})


def test_serialize_rejects_circular_references() -> None:
    loop: dict = {}
    loop["self"] = loop
    with pytest.raises(RecordError):
        serialize_record(loop)
    ring: list = []
    ring.append(ring)
    with pytest.raises(RecordError):
        serialize_record({"ring": ring})


def test_serialize_rejects_absurdly_deep_nesting_without_crashing() -> None:
    deep: list = []
    for _ in range(50000):
        deep = [deep]
    with pytest.raises(RecordError):
        serialize_record({"deep": deep})


def test_serialize_rejects_an_integer_with_too_many_digits() -> None:
    with pytest.raises(RecordError):
        serialize_record({"n": 10**5000})


def test_record_error_is_a_value_error() -> None:
    assert issubclass(RecordError, ValueError)


# --- fsync_directory ---------------------------------------------------------------


def test_fsync_directory_is_a_no_op_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert fsync_directory(tmp_path / "does-not-exist") is None


def test_fsync_directory_opens_syncs_and_closes_the_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple] = []
    fake_os = SimpleNamespace(
        O_RDONLY=0,
        open=lambda path, flags: calls.append(("open", Path(path), flags)) or 77,
        fsync=lambda fd: calls.append(("fsync", fd)),
        close=lambda fd: calls.append(("close", fd)),
    )
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(writer_module, "os", fake_os)
    fsync_directory(tmp_path)
    assert calls == [("open", tmp_path, 0), ("fsync", 77), ("close", 77)]


def test_fsync_directory_closes_the_descriptor_even_when_fsync_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def failing_fsync(fd: int) -> None:
        calls.append("fsync")
        raise OSError("io error")

    fake_os = SimpleNamespace(O_RDONLY=0, open=lambda path, flags: 5, fsync=failing_fsync, close=lambda fd: calls.append("close"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(writer_module, "os", fake_os)
    with pytest.raises(OSError, match="io error"):
        fsync_directory(tmp_path)
    assert calls == ["fsync", "close"]


@pytest.mark.skipif(sys.platform == "win32", reason="directories cannot be opened for fsync on Windows")
def test_fsync_directory_really_works_on_posix(tmp_path: Path) -> None:
    fsync_directory(tmp_path)
    with pytest.raises(OSError):
        fsync_directory(tmp_path / "missing")


# --- construction, path_for, close -------------------------------------------------


def test_writer_properties_and_type_check(tmp_path: Path) -> None:
    w = EvidenceWriter(tmp_path, Durability.PROCESS)
    assert w.directory == tmp_path and w.durability is Durability.PROCESS
    with pytest.raises(TypeError):
        EvidenceWriter(tmp_path, "crash")  # type: ignore[arg-type]


def test_path_for(tmp_path: Path) -> None:
    w = EvidenceWriter(tmp_path, Durability.PROCESS)
    assert w.path_for("a-20250101T120000Z-9f3a1c2e") == tmp_path / "a-20250101T120000Z-9f3a1c2e.jsonl"
    assert w.path_for("_rejections") == tmp_path / "_rejections.jsonl"
    assert w.path_for("x" * 64) == tmp_path / (("x" * 64) + ".jsonl")


@pytest.mark.parametrize("run_id", ["", "x" * 65, "../x", "a/b", "a\\b", ".hidden", "_other", "_rejections.jsonl", "a b", "abc\n", "é", None, 5, b"a"])
def test_path_for_rejects_invalid_run_ids(tmp_path: Path, run_id: object) -> None:
    with pytest.raises(ValueError):
        EvidenceWriter(tmp_path, Durability.PROCESS).path_for(run_id)  # type: ignore[arg-type]


def test_close_is_idempotent_blocks_further_use_and_keeps_no_handle(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path, Durability.PROCESS)
    w.append("r1", [sample_record()])
    (directory / "r1.jsonl").unlink()  # fails on Windows if the writer still held the file open
    w.close()
    w.close()
    with pytest.raises(ValueError, match="closed"):
        w.append("r1", [sample_record()])
    with pytest.raises(ValueError, match="closed"):
        w.append_bytes_durably(directory / "r1.jsonl", b"x")


# --- append: success, framing, content ---------------------------------------------


def test_append_writes_one_line_per_record_and_reads_back_equal(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    records = [
        meta_record(),
        sample_record(seq=0),
        sample_record(seq=1),
        silence_gap(),
        termination_record(note="é\n\u2028 tricky"),
    ]
    result = w.append("r1", records)
    raw = (directory / "r1.jsonl").read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == len(records)
    raw.decode("utf-8")
    assert b"\r" not in raw
    assert [json.loads(line) for line in raw.splitlines()] == records
    assert result.path == directory / "r1.jsonl" and result.record_count == 5 and result.byte_count == len(raw)


def test_sample_record_is_stored_exactly_as_received(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    record = sample_record(seq=7)
    record["sample"]["rss_bytes"] = None
    record["sample"]["null_reasons"] = {"rss_bytes": "cannot read /proc/self/statm: é"}
    w.append("r1", [record])
    stored = json.loads((directory / "r1.jsonl").read_bytes())
    assert stored == record
    assert stored["sample"]["ts"] != stored["recv_time"]  # Capture_Time and Receive_Time both kept


def test_append_is_append_only_and_never_touches_existing_bytes(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    path = directory / "r1.jsonl"
    path.write_bytes(b'{"legacy":1}\n{"legacy":2}\n')
    before = path.read_bytes()
    w.append("r1", [sample_record(seq=0)])
    w.append("r1", [sample_record(seq=1)])
    after = path.read_bytes()
    assert after.startswith(before)
    assert len(after.splitlines()) == 4


def test_append_refuses_a_file_with_an_incomplete_last_line_and_writes_nothing(tmp_path: Path) -> None:
    """Repair-before-append (task 2.10, design D26): the writer never glues a record onto a partial line.
    It does not repair either; `repair_file` does."""
    w, recorder, directory = make(tmp_path)
    path = directory / "r1.jsonl"
    path.write_bytes(b'{"partial":')
    with pytest.raises(TailRepairRequired, match="r1.jsonl does not end with a newline"):
        w.append("r1", [sample_record()])
    assert path.read_bytes() == b'{"partial":' and recorder.events == []
    assert not issubclass(TailRepairRequired, OSError)  # a logic condition, not an I/O failure


def test_one_write_one_flush_for_the_whole_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple] = []
    w, recorder, directory = make(tmp_path)
    recorder.events = log
    monkeypatch.setattr(writer_module, "open", lambda p, m: SpyFile(builtins.open(p, m), log), raising=False)
    w.append("r1", [sample_record(seq=0), sample_record(seq=1), sample_record(seq=2)])
    assert [e[0] for e in log] == ["write", "flush", "fsync_file", "fsync_dir"]
    assert log[0][1] == len((directory / "r1.jsonl").read_bytes())  # one write carried all three lines


def test_empty_batch_touches_nothing(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    result = w.append("r1", [])
    assert result == AppendResult(directory / "r1.jsonl", 0, 0, False, False, False)
    assert result.durability is Durability.PROCESS  # nothing was persisted, so nothing is claimed
    assert not (directory / "r1.jsonl").exists() and recorder.events == []


def test_any_iterable_of_records_is_accepted(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path, Durability.PROCESS)
    w.append("r1", (sample_record(seq=i) for i in range(3)))
    assert len((directory / "r1.jsonl").read_bytes().splitlines()) == 3


def test_rejections_file_takes_rejection_records_with_a_null_run_id(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    rejection = rejection_record()
    result = w.append("_rejections", [rejection])
    assert result.path == directory / "_rejections.jsonl"
    assert json.loads((directory / "_rejections.jsonl").read_bytes()) == rejection


# --- durability levels --------------------------------------------------------------


class SpyFile:
    def __init__(self, real, log: list, fail_flush: bool = False) -> None:
        self._real, self._log, self._fail_flush = real, log, fail_flush

    def __enter__(self) -> "SpyFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        self._log.append(("write", len(data)))
        return self._real.write(data)

    def flush(self) -> None:
        self._log.append(("flush",))
        if self._fail_flush:
            raise OSError("disk full")
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()


def test_crash_durable_new_file_writes_flushes_then_fsyncs_file_then_directory(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path, Durability.CRASH)
    result = w.append("r1", [sample_record(seq=0), sample_record(seq=1)])
    size = (directory / "r1.jsonl").stat().st_size
    assert recorder.events == [("fsync_file", size), ("fsync_dir", directory)]  # all bytes were on disk at fsync time
    assert result.created and result.fsynced_file and result.fsynced_directory
    assert result.durability is Durability.CRASH and w.durability is Durability.CRASH


def test_crash_durable_existing_file_fsyncs_the_file_only(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path, Durability.CRASH)
    w.append("r1", [sample_record(seq=0)])
    recorder.events.clear()
    result = w.append("r1", [sample_record(seq=1)])
    assert recorder.kinds == ["fsync_file"]
    assert recorder.events[0][1] == (directory / "r1.jsonl").stat().st_size
    assert not result.created and result.fsynced_file and not result.fsynced_directory
    assert result.durability is Durability.CRASH  # the directory entry was already durable


def test_crash_durable_fsyncs_after_every_single_append(tmp_path: Path) -> None:
    w, recorder, _ = make(tmp_path, Durability.CRASH)
    for seq in range(5):
        w.append("r1", [sample_record(seq=seq)])
    assert recorder.kinds == ["fsync_file", "fsync_dir"] + ["fsync_file"] * 4


def test_process_durable_never_calls_fsync_but_does_write_and_flush(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path, Durability.PROCESS)
    result = w.append("r1", [sample_record(seq=0)])
    w.append("r1", [sample_record(seq=1)])
    assert recorder.events == []
    assert len((directory / "r1.jsonl").read_bytes().splitlines()) == 2
    assert result.created and not result.fsynced_file and not result.fsynced_directory
    assert result.durability is Durability.PROCESS and w.durability is Durability.PROCESS


def test_process_durable_flushes_before_returning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple] = []
    w, _, _ = make(tmp_path, Durability.PROCESS)
    monkeypatch.setattr(writer_module, "open", lambda p, m: SpyFile(builtins.open(p, m), log), raising=False)
    w.append("r1", [sample_record()])
    assert [e[0] for e in log] == ["write", "flush"]


def test_default_fsync_callables_are_the_real_ones(tmp_path: Path) -> None:
    w = EvidenceWriter(tmp_path, Durability.CRASH)
    result = w.append("r1", [sample_record()])
    assert result.durability is Durability.CRASH and (tmp_path / "r1.jsonl").exists()


@pytest.mark.parametrize(
    "created, fsynced_file, fsynced_dir, count, expected",
    [
        (True, True, True, 1, Durability.CRASH),
        (False, True, False, 1, Durability.CRASH),
        (True, True, False, 1, Durability.PROCESS),  # created but the directory was not synced
        (False, False, False, 1, Durability.PROCESS),
        (True, False, True, 1, Durability.PROCESS),
        (True, True, True, 0, Durability.PROCESS),
    ],
)
def test_append_result_durability_reports_the_steps_actually_done(created: bool, fsynced_file: bool, fsynced_dir: bool, count: int, expected: Durability) -> None:
    assert AppendResult(Path("x"), count, 10, created, fsynced_file, fsynced_dir).durability is expected


# --- failures never produce an acknowledgement-ready result -------------------------


def test_failing_file_fsync_raises_oserror_and_skips_the_directory_fsync(tmp_path: Path) -> None:
    events: list[str] = []

    def bad_fsync(fd: int) -> None:
        events.append("fsync_file")
        raise OSError("fsync failed")

    w = EvidenceWriter(tmp_path, Durability.CRASH, fsync=bad_fsync, fsync_dir=lambda p: events.append("fsync_dir"))
    with pytest.raises(OSError, match="fsync failed"):
        w.append("r1", [sample_record()])
    assert events == ["fsync_file"]


def test_failing_directory_fsync_raises_oserror(tmp_path: Path) -> None:
    def bad_dir(path: Path) -> None:
        raise OSError("dir fsync failed")

    w = EvidenceWriter(tmp_path, Durability.CRASH, fsync=lambda fd: None, fsync_dir=bad_dir)
    with pytest.raises(OSError, match="dir fsync failed"):
        w.append("r1", [sample_record()])


def test_failing_flush_raises_oserror_and_never_reaches_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple] = []
    w, recorder, _ = make(tmp_path)
    recorder.events = log
    monkeypatch.setattr(writer_module, "open", lambda p, m: SpyFile(builtins.open(p, m), log, fail_flush=True), raising=False)
    with pytest.raises(OSError, match="disk full"):
        w.append("r1", [sample_record()])
    assert [e[0] for e in log] == ["write", "flush"]


def test_missing_directory_raises_oserror_and_creates_nothing(tmp_path: Path) -> None:
    w = EvidenceWriter(tmp_path / "no-such-dir", Durability.CRASH, fsync=lambda fd: None, fsync_dir=lambda p: None)
    with pytest.raises(OSError):
        w.append("r1", [sample_record()])
    assert not (tmp_path / "no-such-dir").exists()


def test_a_directory_in_the_way_of_the_file_raises_oserror(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    (directory / "r1.jsonl").mkdir()
    with pytest.raises(OSError):
        w.append("r1", [sample_record()])
    assert recorder.events == []


def test_a_failed_append_does_not_break_the_next_one(tmp_path: Path) -> None:
    state = {"fail": True}

    def flaky(fd: int) -> None:
        if state["fail"]:
            state["fail"] = False
            raise OSError("transient")

    w = EvidenceWriter(tmp_path, Durability.CRASH, fsync=flaky, fsync_dir=lambda p: None)
    with pytest.raises(OSError):
        w.append("r1", [sample_record(seq=0)])
    result = w.append("r1", [sample_record(seq=1)])
    assert result.durability is Durability.CRASH


# --- validation happens before any byte is written -----------------------------------


def nothing_written(directory: Path, recorder: Recorder) -> bool:
    return not list(directory.iterdir()) and recorder.events == []


@pytest.mark.parametrize(
    "mutate, field, code",
    [
        (lambda r: r["sample"].__setitem__("seq", -1), "sample.seq", "out_of_range"),
        (lambda r: r["sample"].__setitem__("rss_bytes", True), "sample.rss_bytes", "wrong_type"),
        (lambda r: r["sample"].__setitem__("ts", "bad"), "sample.ts", "invalid_format"),
        (lambda r: r["sample"].__setitem__("loop_delay_ms", float("nan")), "sample.loop_delay_ms", "out_of_range"),
        (lambda r: r["sample"].__setitem__("extra", 1), "sample.extra", "unknown_field"),
        (lambda r: r["sample"].__setitem__("rss_bytes", None), "sample.rss_bytes", "null_without_reason"),
        (lambda r: r["sample"].pop("pid"), "sample.pid", "missing_field"),
        (lambda r: r.pop("recv_time"), "recv_time", "missing_field"),
        (lambda r: r.__setitem__("schema_version", 2), "schema_version", "out_of_range"),
        (lambda r: r.__setitem__("record_type", "nonsense"), "record_type", "out_of_range"),
        (lambda r: r.__setitem__("note", 1), "note", "unknown_field"),
    ],
)
def test_an_invalid_record_rejects_the_whole_batch_before_any_write(tmp_path: Path, mutate, field: str, code: str) -> None:
    w, recorder, directory = make(tmp_path)
    bad = sample_record(seq=1)
    mutate(bad)
    with pytest.raises(RecordError) as info:
        w.append("r1", [sample_record(seq=0), bad, sample_record(seq=2)])
    assert (field, code) in [(i.field, i.reason_code) for i in info.value.issues]
    assert "record 1" in str(info.value)
    assert nothing_written(directory, recorder)


def test_an_invalid_batch_leaves_an_existing_file_byte_identical(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    w.append("r1", [sample_record(seq=0)])
    before = (directory / "r1.jsonl").read_bytes()
    recorder.events.clear()
    bad = sample_record(seq=1)
    bad["sample"]["seq"] = "1"
    with pytest.raises(RecordError):
        w.append("r1", [sample_record(seq=1), bad])
    assert (directory / "r1.jsonl").read_bytes() == before and recorder.events == []


@pytest.mark.parametrize("bad", [None, "text", 5, ["list"], b"bytes", {"no": "envelope"}, {}])
def test_non_record_items_are_rejected(tmp_path: Path, bad: object) -> None:
    w, recorder, directory = make(tmp_path)
    with pytest.raises(RecordError):
        w.append("r1", [sample_record(), bad])  # type: ignore[list-item]
    assert nothing_written(directory, recorder)


@pytest.mark.parametrize("records", ["text", b"bytes", bytearray(b"x"), sample_record()])
def test_a_single_record_or_text_instead_of_a_sequence_is_rejected(tmp_path: Path, records: object) -> None:
    w, recorder, directory = make(tmp_path)
    with pytest.raises(RecordError, match="sequence of mappings"):
        w.append("r1", records)  # type: ignore[arg-type]
    assert nothing_written(directory, recorder)


def test_a_record_that_cannot_be_serialized_rejects_the_batch(tmp_path: Path) -> None:
    """Since task 2.19 every field is typed, so validation stops these before serialization; serialize_record's own errors are tested above."""
    w, recorder, directory = make(tmp_path)
    unserializable = envelope("repair", payload={"values": {1, 2}})
    with pytest.raises(RecordError, match="record 1 is invalid"):
        w.append("r1", [sample_record(), unserializable])
    assert nothing_written(directory, recorder)
    non_string_key = {**termination_record(), "container_state": {1: "x"}}
    with pytest.raises(RecordError, match="record 0 is invalid"):
        w.append("r1", [non_string_key])
    assert nothing_written(directory, recorder)


def test_a_record_for_another_run_is_rejected(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    with pytest.raises(RecordError, match="expected 'r1'"):
        w.append("r1", [sample_record("r1"), sample_record("other")])
    assert nothing_written(directory, recorder)


def test_a_null_run_id_does_not_belong_in_a_run_file(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    with pytest.raises(RecordError):
        w.append("r1", [rejection_record(None)])
    assert nothing_written(directory, recorder)


@pytest.mark.parametrize(
    "record",
    [sample_record("r1"), rejection_record("r1"), repair_rec("r1"), {**silence_gap(), "run_id": None}, meta_record("r1")],
)
def test_the_rejections_file_takes_only_rejections_with_null_run_id(tmp_path: Path, record: dict) -> None:
    w, recorder, directory = make(tmp_path)
    with pytest.raises(RecordError):
        w.append("_rejections", [record])
    assert nothing_written(directory, recorder)


def test_an_invalid_run_id_is_rejected_before_anything_else(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    for run_id in ("../escape", "a/b", "", "_other", "x" * 65):
        with pytest.raises(ValueError):
            w.append(run_id, [sample_record()])
    assert nothing_written(directory, recorder)
    assert not (tmp_path / "escape.jsonl").exists()


def test_non_sample_payloads_are_validated_since_task_2_19(tmp_path: Path) -> None:
    """A bad payload of any of the six non-sample types rejects the whole batch and writes nothing."""
    w, recorder, directory = make(tmp_path, Durability.PROCESS)
    for kind in ("run_metadata", "gap", "sequence_anomaly", "rejection", "repair", "termination"):
        garbage = envelope(kind, payload_is_not_validated=[1, "x", None])
        with pytest.raises(RecordError, match="record 1 is invalid"):
            w.append("r1", [sample_record(), garbage])
    assert not list(directory.iterdir())
    good = [meta_record(), silence_gap(), termination_record()]
    assert w.append("r1", good).record_count == 3
    assert len((directory / "r1.jsonl").read_bytes().splitlines()) == 3


# --- append_bytes_durably ----------------------------------------------------------


def test_append_bytes_durably_crash_new_file(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path, Durability.CRASH)
    path = directory / "r1.jsonl"
    w.append_bytes_durably(path, b"\n" + serialize_record(envelope("repair")))
    assert recorder.events == [("fsync_file", path.stat().st_size), ("fsync_dir", directory)]


def test_append_bytes_durably_crash_existing_file_syncs_the_file_only(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path, Durability.CRASH)
    path = directory / "r1.jsonl"
    path.write_bytes(b"existing\n")
    w.append_bytes_durably(path, b"more\n")
    assert recorder.kinds == ["fsync_file"]
    assert path.read_bytes() == b"existing\nmore\n"


def test_append_bytes_durably_process_never_fsyncs(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path, Durability.PROCESS)
    w.append_bytes_durably(directory / "r1.jsonl", b"abc")
    assert recorder.events == [] and (directory / "r1.jsonl").read_bytes() == b"abc"


def test_append_bytes_durably_does_not_validate_or_alter_the_bytes(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    payload = b"\xff\xfe not json \x00\n"
    w.append_bytes_durably(directory / "r1.jsonl", payload)
    w.append_bytes_durably(directory / "r1.jsonl", bytearray(b"x"))
    w.append_bytes_durably(directory / "r1.jsonl", memoryview(b"y"))
    assert (directory / "r1.jsonl").read_bytes() == payload + b"xy"


def test_append_bytes_durably_with_no_data_does_nothing(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    w.append_bytes_durably(directory / "r1.jsonl", b"")
    assert not (directory / "r1.jsonl").exists() and recorder.events == []


def test_append_bytes_durably_failure_raises_oserror(tmp_path: Path) -> None:
    def bad(fd: int) -> None:
        raise OSError("fsync failed")

    w = EvidenceWriter(tmp_path, Durability.CRASH, fsync=bad, fsync_dir=lambda p: None)
    with pytest.raises(OSError, match="fsync failed"):
        w.append_bytes_durably(tmp_path / "r1.jsonl", b"x\n")


def test_append_bytes_durably_accepts_files_below_the_directory(tmp_path: Path) -> None:
    w, recorder, directory = make(tmp_path)
    sub = directory / ".coroner_probe"
    sub.mkdir()
    w.append_bytes_durably(sub / "probe.tmp", b"x")
    assert recorder.events == [("fsync_file", 1), ("fsync_dir", sub)]


@pytest.mark.parametrize("where", ["sibling", "parent", "traversal"])
def test_append_bytes_durably_refuses_paths_outside_the_directory(tmp_path: Path, where: str) -> None:
    w, recorder, directory = make(tmp_path)
    target = {
        "sibling": tmp_path / "elsewhere.jsonl",
        "parent": directory.parent / "x.jsonl",
        "traversal": directory / ".." / "x.jsonl",
    }[where]
    with pytest.raises(ValueError, match="outside"):
        w.append_bytes_durably(target, b"x")
    assert recorder.events == [] and not (tmp_path / "x.jsonl").exists() and not (tmp_path / "elsewhere.jsonl").exists()


@pytest.mark.parametrize("data", ["text", 5, None, ["x"]])
def test_append_bytes_durably_requires_bytes(tmp_path: Path, data: object) -> None:
    w, _, directory = make(tmp_path)
    with pytest.raises(TypeError):
        w.append_bytes_durably(directory / "r1.jsonl", data)  # type: ignore[arg-type]


# --- the writer leaves no stray files -----------------------------------------------


def test_only_the_run_files_exist_after_appends(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    w.append("r1", [sample_record("r1")])
    w.append("r2", [sample_record("r2")])
    w.append("_rejections", [rejection_record()])
    assert sorted(p.name for p in directory.iterdir()) == ["_rejections.jsonl", "r1.jsonl", "r2.jsonl"]


def test_records_for_different_runs_go_to_different_files(tmp_path: Path) -> None:
    w, _, directory = make(tmp_path)
    for seq in range(3):
        w.append("r1", [sample_record("r1", seq)])
        w.append("r2", [sample_record("r2", seq)])
    for run in ("r1", "r2"):
        stored = [json.loads(line) for line in (directory / f"{run}.jsonl").read_bytes().splitlines()]
        assert [r["run_id"] for r in stored] == [run] * 3

# =====================================================================================
# Added by task 2.5 (gaps found by the coverage audit)
# =====================================================================================


class PartialFile(SpyFile):
    """Writes only the first `limit` bytes of each write, then raises OSError, like a full disk."""

    def __init__(self, real, log: list, limit: int) -> None:
        super().__init__(real, log)
        self._limit = limit

    def write(self, data: bytes) -> int:
        self._log.append(("write", len(data)))
        self._real.write(data[: self._limit])
        self._real.flush()
        raise OSError("no space left on device")


def use_partial_writes(monkeypatch: pytest.MonkeyPatch, limit: int, log: list) -> None:
    monkeypatch.setattr(writer_module, "open", lambda p, m: PartialFile(builtins.open(p, m), log, limit), raising=False)


def test_a_write_that_fails_part_way_raises_leaves_a_partial_line_and_never_syncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w, recorder, directory = make(tmp_path)
    line = serialize_record(sample_record(seq=0))
    use_partial_writes(monkeypatch, limit=len(line) // 2, log=[])
    with pytest.raises(OSError, match="no space"):
        w.append("r1", [sample_record(seq=0)])
    stored = (directory / "r1.jsonl").read_bytes()
    assert stored == line[: len(line) // 2] and not stored.endswith(b"\n")  # a partial line stays: no atomicity
    assert recorder.events == []  # neither fsync ran, so nothing was reported as durable


def test_a_failed_batch_can_leave_some_complete_lines_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No atomicity across the records of one append: lines 0 and 1 reach the file, line 2 does not."""
    w, recorder, directory = make(tmp_path)
    batch = [sample_record(seq=0), sample_record(seq=1), sample_record(seq=2)]
    lines = [serialize_record(r) for r in batch]
    use_partial_writes(monkeypatch, limit=len(lines[0]) + len(lines[1]), log=[])
    with pytest.raises(OSError):
        w.append("r1", batch)
    assert (directory / "r1.jsonl").read_bytes() == lines[0] + lines[1]
    assert recorder.events == []


def test_a_failed_write_that_stops_just_before_the_newline_leaves_valid_json_without_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w, _, directory = make(tmp_path)
    line = serialize_record(sample_record(seq=0))
    use_partial_writes(monkeypatch, limit=len(line) - 1, log=[])
    with pytest.raises(OSError):
        w.append("r1", [sample_record(seq=0)])
    stored = (directory / "r1.jsonl").read_bytes()
    assert not stored.endswith(b"\n")
    assert json.loads(stored) == sample_record(seq=0)  # parses, but was never acknowledged


def test_after_a_partial_write_the_next_append_is_refused_until_the_tail_is_repaired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w, recorder, directory = make(tmp_path)
    line = serialize_record(sample_record(seq=0))
    with monkeypatch.context() as patch:
        use_partial_writes(patch, limit=len(line) - 1, log=[])
        with pytest.raises(OSError):
            w.append("r1", [sample_record(seq=0)])
    before = (directory / "r1.jsonl").read_bytes()
    recorder.events.clear()
    with pytest.raises(TailRepairRequired):
        w.append("r1", [sample_record(seq=1)])
    assert (directory / "r1.jsonl").read_bytes() == before and recorder.events == []


def test_a_write_call_that_raises_before_writing_anything_returns_no_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w, recorder, directory = make(tmp_path)
    use_partial_writes(monkeypatch, limit=0, log=[])
    with pytest.raises(OSError):
        w.append("r1", [sample_record()])
    assert (directory / "r1.jsonl").read_bytes() == b"" and recorder.events == []


@pytest.mark.parametrize("failing_step", ["write", "flush", "fsync_file", "fsync_dir"])
def test_no_step_failure_ever_yields_a_result_or_a_crash_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_step: str) -> None:
    log: list[tuple] = []

    def fsync(fd: int) -> None:
        log.append(("fsync_file",))
        if failing_step == "fsync_file":
            raise OSError("fsync failed")

    def fsync_dir(path: Path) -> None:
        log.append(("fsync_dir",))
        if failing_step == "fsync_dir":
            raise OSError("dir fsync failed")

    def opener(path: Path, mode: str) -> SpyFile:
        spy = SpyFile(builtins.open(path, mode), log, fail_flush=failing_step == "flush")
        if failing_step == "write":
            def broken_write(data: bytes) -> int:
                log.append(("write", len(data)))
                raise OSError("write failed")
            spy.write = broken_write  # type: ignore[method-assign]
        return spy

    monkeypatch.setattr(writer_module, "open", opener, raising=False)
    w = EvidenceWriter(tmp_path, Durability.CRASH, fsync=fsync, fsync_dir=fsync_dir)
    result = None
    with pytest.raises(OSError):
        result = w.append("r1", [sample_record()])
    assert result is None
    order = [event[0] for event in log]
    expected = {
        "write": ["write"],
        "flush": ["write", "flush"],
        "fsync_file": ["write", "flush", "fsync_file"],
        "fsync_dir": ["write", "flush", "fsync_file", "fsync_dir"],
    }[failing_step]
    assert order == expected  # the failing step is the last one attempted


def test_append_bytes_durably_does_not_claim_success_when_the_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w, recorder, directory = make(tmp_path)
    use_partial_writes(monkeypatch, limit=1, log=[])
    with pytest.raises(OSError):
        w.append_bytes_durably(directory / "r1.jsonl", b"\nrepair-record\n")
    assert recorder.events == []


# --- serialization never silently changes a value ------------------------------------


def test_float_values_survive_serialization_exactly() -> None:
    values = [0.1, 1 / 3, 2 / 3, 1e-5, 5e-324, 1.7976931348623157e308, 123456789.123456789, 0.84, 1e22, 1e23, -2.5e-8]
    again = json.loads(serialize_record({"v": values}))["v"]
    assert again == values and all(isinstance(x, float) for x in again)


def test_negative_zero_and_integral_floats_keep_their_type_and_sign() -> None:
    import math

    data = serialize_record({"nz": -0.0, "one": 1.0, "i": 1})
    assert data == b'{"nz":-0.0,"one":1.0,"i":1}\n'
    again = json.loads(data)
    assert math.copysign(1.0, again["nz"]) == -1.0
    assert isinstance(again["one"], float) and isinstance(again["i"], int)


def test_integers_of_any_size_below_the_digit_limit_survive() -> None:
    values = [0, -1, 2**31, 2**63, 2**64, 2**200, -(2**200), 10**1000]
    assert json.loads(serialize_record({"v": values}))["v"] == values


def test_strings_are_not_normalized_trimmed_or_re_encoded() -> None:
    values = ["  padded  ", "e\u0301 (combining)", "\u00e9 (precomposed)", "\ufeff bom", "\u200b zero width", "line\nbreak", "\x00\x01\x1f"]
    again = json.loads(serialize_record({"v": values}))["v"]
    assert again == values
    assert [len(x) for x in again] == [len(x) for x in values]  # no Unicode normalization


def test_booleans_and_null_are_not_turned_into_numbers_or_strings() -> None:
    again = json.loads(serialize_record({"t": True, "f": False, "n": None, "z": 0, "o": 1}))
    assert again["t"] is True and again["f"] is False and again["n"] is None
    assert again["z"] == 0 and again["z"] is not False and again["o"] == 1 and again["o"] is not True


def test_tuples_are_written_as_json_arrays_and_read_back_as_lists() -> None:
    """Pinned JSON behaviour: the record's tuple becomes a list after a round trip. Producers use lists."""
    assert serialize_record({"t": (1, 2, (3,))}) == b'{"t":[1,2,[3]]}\n'
    assert json.loads(serialize_record({"t": (1, 2)})) == {"t": [1, 2]}


def test_duplicate_looking_keys_are_not_merged() -> None:
    assert json.loads(serialize_record({"a": 1, "A": 2, "a ": 3})) == {"a": 1, "A": 2, "a ": 3}


# --- reserved filename and run-id collisions -------------------------------------------


def test_no_valid_run_id_can_name_a_reserved_file(tmp_path: Path) -> None:
    """Exhaustive over all strings of length 0 to 4 from an alphabet that mixes valid and hostile characters."""
    import itertools

    from coroner.evidence.schema import validate_run_id

    w = EvidenceWriter(tmp_path, Durability.PROCESS)
    reserved = w.path_for("_rejections")
    valid_count = 0
    for length in range(0, 5):
        for chars in itertools.product("a1_.-/\\", repeat=length):
            run_id = "".join(chars)
            if validate_run_id(run_id) is not None:
                if run_id != "_rejections":
                    with pytest.raises(ValueError):
                        w.path_for(run_id)
                continue
            valid_count += 1
            path = w.path_for(run_id)
            assert path != reserved
            assert path.parent == tmp_path and path.name == run_id + ".jsonl"
            assert not path.name.startswith(("_", "."))
    assert valid_count > 100


@pytest.mark.parametrize("name", ["_rejections", "_rejections.jsonl", "_REJECTIONS", "_Rejections", ".coroner_probe", ".collector.lock", "_x", ".x"])
def test_reserved_and_dot_names_are_not_valid_run_ids(tmp_path: Path, name: str) -> None:
    from coroner.evidence.schema import validate_run_id

    assert validate_run_id(name) is not None
    if name != "_rejections":
        with pytest.raises(ValueError):
            EvidenceWriter(tmp_path, Durability.PROCESS).path_for(name)


def test_a_run_id_ending_in_the_file_suffix_gets_a_distinct_file(tmp_path: Path) -> None:
    w = EvidenceWriter(tmp_path, Durability.PROCESS)
    assert w.path_for("x") != w.path_for("x.jsonl")
    assert w.path_for("x.jsonl").name == "x.jsonl.jsonl"


def test_case_variants_are_distinct_paths_so_a_case_insensitive_volume_would_merge_them(tmp_path: Path) -> None:
    """Valid ids 'Run1' and 'run1' are distinct names here. The Collector's Linux volume keeps them apart;
    a case-insensitive filesystem (Windows or macOS) would not. The runner generates lower-case ids (design D1)."""
    w = EvidenceWriter(tmp_path, Durability.PROCESS)
    # Compare names as strings: on Windows, Path equality itself ignores case.
    assert w.path_for("Run1").name != w.path_for("run1").name
    assert w.path_for("Run1").name.lower() == w.path_for("run1").name.lower()

# --- a failed first append must not let a retry skip the directory fsync -------------


def flaky_writer(tmp_path: Path, fail_file_fsyncs: int = 0, fail_dir_fsyncs: int = 0) -> tuple[EvidenceWriter, list[str]]:
    events: list[str] = []
    budget = {"file": fail_file_fsyncs, "dir": fail_dir_fsyncs}

    def fsync(fd: int) -> None:
        events.append("fsync_file")
        if budget["file"] > 0:
            budget["file"] -= 1
            raise OSError("fsync failed")

    def fsync_dir(path: Path) -> None:
        events.append("fsync_dir")
        if budget["dir"] > 0:
            budget["dir"] -= 1
            raise OSError("dir fsync failed")

    return EvidenceWriter(tmp_path, Durability.CRASH, fsync=fsync, fsync_dir=fsync_dir), events


def test_retry_after_a_failed_file_fsync_still_syncs_the_directory_entry(tmp_path: Path) -> None:
    w, events = flaky_writer(tmp_path, fail_file_fsyncs=1)
    with pytest.raises(OSError):
        w.append("r1", [sample_record(seq=0)])  # creates the file, then the file fsync fails
    assert events == ["fsync_file"]
    result = w.append("r1", [sample_record(seq=1)])  # the file already exists, but its entry was never synced
    assert events == ["fsync_file", "fsync_file", "fsync_dir"]
    assert not result.created and result.fsynced_directory and result.durability is Durability.CRASH
    w.append("r1", [sample_record(seq=2)])  # now steady state: the directory is not synced again
    assert events[3:] == ["fsync_file"]


def test_retry_after_a_failed_directory_fsync_syncs_the_directory_again(tmp_path: Path) -> None:
    w, events = flaky_writer(tmp_path, fail_dir_fsyncs=1)
    with pytest.raises(OSError, match="dir fsync failed"):
        w.append("r1", [sample_record(seq=0)])
    result = w.append("r1", [sample_record(seq=1)])
    assert events == ["fsync_file", "fsync_dir", "fsync_file", "fsync_dir"]
    assert result.fsynced_directory and result.durability is Durability.CRASH
    w.append("r1", [sample_record(seq=2)])
    assert events[4:] == ["fsync_file"]


def test_retry_after_a_failed_write_that_created_the_file_still_syncs_the_directory_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w, events = flaky_writer(tmp_path)
    with monkeypatch.context() as patch:
        use_partial_writes(patch, limit=0, log=[])  # the file is created and stays empty, so a retry is allowed
        with pytest.raises(OSError):
            w.append("r1", [sample_record(seq=0)])
    assert events == []
    w.append("r1", [sample_record(seq=1)])
    assert events == ["fsync_file", "fsync_dir"]


def test_retry_via_append_bytes_durably_also_syncs_the_directory_entry(tmp_path: Path) -> None:
    w, events = flaky_writer(tmp_path, fail_file_fsyncs=1)
    path = tmp_path / "r1.jsonl"
    with pytest.raises(OSError):
        w.append_bytes_durably(path, b"first\n")
    w.append_bytes_durably(path, b"second\n")
    assert events == ["fsync_file", "fsync_file", "fsync_dir"]


def test_process_durable_never_syncs_a_directory_even_after_a_failed_first_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w, recorder, _ = make(tmp_path, Durability.PROCESS)
    with monkeypatch.context() as patch:
        use_partial_writes(patch, limit=0, log=[])
        with pytest.raises(OSError):
            w.append("r1", [sample_record(seq=0)])
    result = w.append("r1", [sample_record(seq=1)])
    assert recorder.events == [] and result.durability is Durability.PROCESS


def test_a_file_that_already_existed_before_the_writer_is_not_directory_synced(tmp_path: Path) -> None:
    """Known limit: the writer only tracks files it created itself. Startup code (the Collector) owns older files."""
    (tmp_path / "r1.jsonl").write_bytes(b"")
    w, events = flaky_writer(tmp_path)
    w.append("r1", [sample_record()])
    assert events == ["fsync_file"]