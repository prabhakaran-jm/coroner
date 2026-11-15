"""Regression tests for the D30 invalidating marker (correction to task 2.10, found in task 2.16).

For an UNTERMINATED last line, `repair_file` appends ONE buffer: a NUL marker, the isolating newline, then the
repair record. Whatever prefix of that buffer reaches the file (a crash, a short write, a failed flush or fsync),
the never-acknowledged original line must never be read as a valid record, the original bytes must stay an
unchanged prefix, and retries must converge to a stable, idempotent file.

**Validates: Requirements 5.5, 5.6, 5.7, 5.8, 5.10, 13.13**

Independent oracle: expected records, kinds, line lengths and the buffer length are computed here from the
original bytes with `json`, `base64` and `bytes` operations only (never `repair_file`, `inspect_tail` or a writer
helper). The caps are restated as literals.
"""
from __future__ import annotations

import base64
import builtins
import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from coroner.evidence import writer as writer_module
from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.repair import repair_file
from coroner.evidence.writer import Durability, EvidenceWriter

CAP = 1024 * 1024
LINE_CAP = 2 * 1024 * 1024
NOW = "2025-01-01T12:05:00.004Z"
RECV = "2025-01-01T12:00:06.530Z"
MARK = b"\x00"


def now() -> str:
    return NOW


# --- independent oracle -------------------------------------------------------------------------------


def _no_constant(name: str):
    raise ValueError(name)


def classify(raw: bytes) -> str | None:
    """Reader kind for a terminated line that is not a valid record; None if it parses to an object."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid_utf8"
    try:
        value = json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return "invalid_json"
    return None if isinstance(value, dict) else "not_an_object"


def valid_object(raw: bytes) -> bool:
    return len(raw) <= LINE_CAP and classify(raw) is None


def line_of(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"


def expected_record(seen: bytes, line_no: int, file: str, run_id: str | None, terminated: bool) -> dict:
    """The repair record a repair attempt must write for the last line `seen`."""
    if terminated:
        reason, action = "invalid_json_last_line", "none_needed_already_terminated"
    else:
        reason = "missing_trailing_newline" if valid_object(seen) else "incomplete_last_line"
        action = "appended_newline"
    return {
        "record_type": "repair", "schema_version": 1, "run_id": run_id, "recv_time": NOW, "file": file,
        "line_no": line_no, "fragment_len": len(seen), "fragment_b64": base64.b64encode(seen[:CAP]).decode("ascii"),
        "fragment_truncated": len(seen) > CAP, "reason": reason, "action": action,
    }


def sample(seq: int) -> dict:
    body = {
        "run_id": "r1", "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 71303168, "request_count": 9,
        "loop_delay_ms": 0.84, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }
    return {"record_type": "sample", "schema_version": 1, "run_id": "r1", "recv_time": RECV, "sample": body}


def rejection() -> dict:
    return {"record_type": "rejection", "schema_version": 1, "run_id": None, "recv_time": RECV, "http_status": 422, "reason_code": "invalid_json"}


RUN_PREFIX = line_of(sample(0)) + line_of(sample(1))
REJ_PREFIX = line_of(rejection()) + line_of(rejection())
SAMPLE_BODY = line_of(sample(7))[:-1]  # a complete valid Sample that lost only its newline
FRAGMENTS = {
    "sample": SAMPLE_BODY,
    "obj": b'{"a":1}',
    "partial": SAMPLE_BODY[:40],
    "badutf8": b'{"a":"\xff\xfe',
    "nul": b"\x00",
}


# --- IO doubles -----------------------------------------------------------------------------------------


class Spy:
    """An append handle that writes at most `limit` bytes and then raises, or fails flush; logs every call."""

    def __init__(self, real, env: "Env") -> None:
        self._real, self._env = real, env

    def __enter__(self) -> "Spy":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        self._env.events.append(("write", len(data)))
        self._env.buffers.append(bytes(data))
        if self._env.limit is None:
            return self._real.write(data)
        self._real.write(data[: self._env.limit])
        self._real.flush()
        raise OSError("interrupted write")

    def flush(self) -> None:
        self._env.events.append(("flush",))
        if self._env.fail_flush:
            raise OSError("flush failed")
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()


class Env:
    def __init__(self, root: Path, name: str) -> None:
        self.directory = root / "evidence"
        self.directory.mkdir()
        self.path = self.directory / name
        self.events: list[tuple] = []
        self.buffers: list[bytes] = []
        self.limit: int | None = None
        self.fail_flush = False
        self.fail_fsync = False
        self.writer = EvidenceWriter(self.directory, Durability.CRASH, fsync=self._fsync, fsync_dir=lambda p: None)

    def _fsync(self, fd: int) -> None:
        if self.fail_fsync:
            self.events.append(("fsync_fail",))
            raise OSError("fsync failed")
        self.events.append(("fsync", os.fstat(fd).st_size))

    @contextlib.contextmanager
    def patched(self):
        def opener(p, m):
            return Spy(builtins.open(p, m), self) if m == "ab" else builtins.open(p, m)

        with mock.patch.object(writer_module, "open", opener, create=True):
            yield

    def repair(self) -> dict | None:
        """One healthy, recorded repair. Asserts Crash_Durable order when a record is returned."""
        self.limit, self.fail_flush, self.fail_fsync = None, False, False
        mark = len(self.events)
        with self.patched():
            record = repair_file(self.path, self.writer, now)
        size = self.path.stat().st_size
        if record is None:
            assert self.events[mark:] == []
        else:  # one write, then flush, then fsync of the whole file, all before the record was returned
            assert [e[0] for e in self.events[mark:]] == ["write", "flush", "fsync"]
            assert self.events[mark + 2][1] == size
        return record


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode(record: dict) -> bytes:
    return base64.b64decode(record["fragment_b64"], validate=True)


# --- the scenario check ----------------------------------------------------------------------------------


def check(fragment: bytes, cut: int | None, *, name: str = "r1.jsonl", fault: str = "write") -> dict:
    """Interrupt the first repair of PREFIX + fragment after `cut` bytes (None: no interruption), then retry.

    Returns facts about the outcome for extra assertions."""
    assert fragment and b"\n" not in fragment
    rejections = name == "_rejections.jsonl"
    prefix = REJ_PREFIX if rejections else RUN_PREFIX
    run_id = None if rejections else "r1"
    acknowledged = [] if rejections else [0, 1]
    original = prefix + fragment
    first = expected_record(fragment, 3, name, run_id, False)
    buffer = MARK + b"\n" + line_of(first)  # oracle buffer: its length does not depend on key order
    total = len(buffer)
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp), name)
        env.path.write_bytes(original)
        digest = sha(original)

        def assert_readable(data: bytes) -> None:
            result = read_evidence(data, name=name)
            samples = [r["sample"]["seq"] for r in result.records if r["record_type"] == "sample"]
            assert samples == acknowledged and summarize(result).sample_count == len(acknowledged)
            assert result.line_numbers[:2] == [1, 2] and result.records[:2] == [json.loads(x) for x in prefix.split(b"\n")[:2]]
            assert 3 in {e.line_no for e in result.errors} and 3 not in result.line_numbers

        # the interrupted (or complete) first attempt
        if cut is None:
            assert env.repair() == first
            cut = total
        else:
            env.limit = cut if fault == "write" else None
            env.fail_flush, env.fail_fsync = fault == "flush", fault == "fsync"
            with env.patched(), pytest.raises(OSError):  # no record is returned on any failure
                repair_file(env.path, env.writer, now)
            if fault == "write":
                assert not any(e[0] == "fsync" for e in env.events)
            if fault != "write":
                cut = total
        sent = env.buffers[0]
        assert sent[:2] == MARK + b"\n" and json.loads(sent[2:]) == first and len(sent) == total
        mid = env.path.read_bytes()
        assert mid == original + sent[:cut] and sha(mid[: len(original)]) == digest
        assert_readable(mid)  # D30: never the original line, at any interruption point

        # retries converge: exactly one more repair for a cut inside the buffer, none for a complete buffer
        records = []
        for _ in range(4):  # bounded
            record = env.repair()
            if record is None:
                break
            records.append(record)
        assert len(records) == (0 if cut == total else 1)
        stable = env.path.read_bytes()
        for _ in range(2):
            assert env.repair() is None and env.path.read_bytes() == stable
        assert stable.startswith(mid) and sha(stable[: len(original)]) == digest
        assert_readable(stable)

        result = read_evidence(env.path)
        errors = {e.line_no: e for e in result.errors}
        line3 = stable.split(b"\n")[2]
        assert line3.startswith(fragment)  # the original fragment bytes start the physical line
        retry = records[0] if records else None
        if cut in (0, total):
            assert line3 == fragment + MARK and (retry is None or retry == first)
            expect3 = (len(fragment) + 1, "isolated_by_repair", True)
            assert first in result.records
        elif cut == 1:  # only the marker persisted: the retry sees fragment + NUL and appends another NUL
            assert line3 == fragment + MARK + MARK
            assert retry == expected_record(fragment + MARK, 3, name, run_id, False)
            assert decode(retry).startswith(fragment[:CAP]) and retry["fragment_len"] == len(fragment) + 1
            expect3 = (len(fragment) + 2, "isolated_by_repair", True)
        elif cut == 2:  # marker and newline persisted: the line is terminated and invalid
            assert line3 == fragment + MARK
            assert retry == expected_record(fragment + MARK, 3, name, run_id, True)
            expect3 = (len(fragment) + 1, classify(fragment + MARK), True)
        else:  # part of the record persisted: the retry isolates that partial record line instead
            assert line3 == fragment + MARK
            partial = sent[2:cut]
            assert retry == expected_record(partial, 4, name, run_id, False)
            assert stable.split(b"\n")[3] == partial + MARK
            assert (errors[4].byte_length, errors[4].kind, errors[4].has_repair_record) == (len(partial) + 1, "isolated_by_repair", True)
            if cut == total - 1:  # the complete first record is recoverable from the retry's audit copy
                assert retry["reason"] == "missing_trailing_newline"
                if not retry["fragment_truncated"]:
                    assert json.loads(decode(retry)) == first
            expect3 = (len(fragment) + 1, classify(fragment + MARK), False)
        e3 = errors[3]
        assert (e3.file, e3.byte_length, e3.kind, e3.has_repair_record) == (name, *expect3)
        assert expect3[1] in ("isolated_by_repair", "invalid_json", "invalid_utf8")

        # the file now takes the next record on a fresh line
        if not rejections:
            env.writer.append("r1", [sample(2)])
            assert env.path.read_bytes() == stable + line_of(sample(2))
            assert summarize(read_evidence(env.path)).sample_count == 3
        return {"total": total, "stable": stable}


def cuts_for(total: int) -> dict[str, int | None]:
    return {"0": 0, "1": 1, "2": 2, "3": 3, "10": 10, "mid": total // 2, "t-3": total - 3, "t-2": total - 2,
            "t-1": total - 1, "full": None}


def total_for(fragment: bytes, name: str = "r1.jsonl") -> int:
    run_id = None if name == "_rejections.jsonl" else "r1"
    return 2 + len(line_of(expected_record(fragment, 3, name, run_id, False)))


@pytest.mark.parametrize("where", list(cuts_for(100)))
@pytest.mark.parametrize("frag", sorted(FRAGMENTS))
def test_every_interruption_point_keeps_the_original_and_never_accepts_it(frag: str, where: str) -> None:
    fragment = FRAGMENTS[frag]
    check(fragment, cuts_for(total_for(fragment))[where])


@pytest.mark.parametrize("where", ["0", "1", "2", "mid", "t-1", "full"])
def test_the_rejections_file_gets_null_run_id_records_at_every_point(where: str) -> None:
    fragment = line_of(rejection())[:-1]
    check(fragment, cuts_for(total_for(fragment, "_rejections.jsonl"))[where], name="_rejections.jsonl")


@pytest.mark.parametrize("fault", ["flush", "fsync"])
@pytest.mark.parametrize("frag", ["sample", "partial"])
def test_flush_and_fsync_failures_return_no_record_and_leave_a_quarantined_line(frag: str, fault: str) -> None:
    check(FRAGMENTS[frag], 0, fault=fault)


def test_every_single_cut_of_a_valid_sample_repair() -> None:
    fragment = SAMPLE_BODY
    for cut in range(total_for(fragment) + 1):
        check(fragment, cut)


def big_object(size: int) -> bytes:
    return b'{"k":"' + b"v" * (size - 8) + b'"}'


BIG = {"q-cap+10": b"q" * (CAP + 10), "obj-cap+100": big_object(CAP + 100), "q-2m+10": b"q" * (LINE_CAP + 10)}


@pytest.mark.parametrize("where", ["1", "2", "mid", "t-1", "full"])
@pytest.mark.parametrize("big", sorted(BIG))
def test_large_fragments_over_one_and_two_mebibytes(big: str, where: str) -> None:
    fragment = BIG[big]
    total = total_for(fragment)
    assert total - 2 < LINE_CAP  # the worst-case repair record still fits under the 2 MiB tail limit
    facts = check(fragment, cuts_for(total)[where])
    if where == "full":
        record = json.loads(facts["stable"].split(b"\n")[3])
        assert record["fragment_len"] == len(fragment) and record["fragment_truncated"] is True
        assert decode(record) == fragment[:CAP]


def test_the_marker_makes_every_valid_object_invalid_json() -> None:
    for raw in (b"{}", b'{"a":1}', b' {"a":1} ', b'{"a":1}\r', SAMPLE_BODY, line_of(rejection())[:-1], big_object(CAP)):
        assert classify(raw) is None
        assert classify(raw + MARK) == "invalid_json"
        with pytest.raises(json.JSONDecodeError, match="Extra data"):
            json.loads(raw + MARK)
        assert classify(raw + MARK + MARK) == "invalid_json"


def test_a_terminated_invalid_line_gets_no_marker_and_no_newline() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp), "r1.jsonl")
        original = RUN_PREFIX + b'{"half\n'
        env.path.write_bytes(original)
        record = env.repair()
        assert record == expected_record(b'{"half', 3, "r1.jsonl", "r1", True)
        assert env.path.read_bytes() == original + line_of(record)
        assert env.repair() is None


arbitrary = st.binary(min_size=1, max_size=200).map(lambda b: b.replace(b"\n", b"\x0b"))
json_objects = st.dictionaries(st.text(max_size=6), st.integers() | st.text(max_size=6) | st.none(), max_size=3).map(
    lambda d: json.dumps(d).encode("utf-8")
)


@settings(max_examples=150, deadline=None, derandomize=True, database=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.one_of(arbitrary, json_objects, st.just(SAMPLE_BODY)), st.integers(0, 10**6), st.booleans())
def test_property_any_fragment_any_cut_is_never_accepted_and_converges(fragment: bytes, cut: int, interrupted: bool) -> None:
    total = total_for(fragment)
    check(fragment, cut % (total + 1) if interrupted else None)
