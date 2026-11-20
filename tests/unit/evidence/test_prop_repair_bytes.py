"""Property 8: repair never loses or alters bytes (task 2.15).

For any evidence file whose tail is an incomplete line (arbitrary bytes after the last newline, including a
valid JSON object missing its newline, truncated JSON, and invalid UTF-8) or an invalid last line, after
`repair_file`: the original bytes are an unmodified prefix of the repaired file, the `repair` record describes
the original fragment correctly, the repair was persisted (fsync under Crash_Durable) before a later append,
and the next appended record begins on a fresh line.

**Validates: Requirements 5.5, 5.6, 5.7, 5.8, 13.13**

Oracle: `oracle()` below recomputes the expected record from the original bytes with plain `bytes` operations and
`json`. It never calls `repair_file`, `inspect_tail` or a writer helper. The cap values are restated here as
literals on purpose (and checked against the production constants once).

Failure injection writes a prefix of the repair append and then raises, or fails flush or fsync. No atomicity is
claimed: the tests only assert that the original prefix survives, that no success is reported, and that retries
converge. Since D30 an unterminated fragment is followed by one NUL invalidating marker, then the isolating newline,
then the record, so the physical isolated line is the fragment plus one NUL (not byte-identical to the fragment),
and an interruption after the marker or after the newline can no longer make a never-acknowledged JSON object
look like a valid record. The reader never counts more Samples than the complete original lines hold.
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

from coroner.evidence import repair as repair_module
from coroner.evidence import writer as writer_module
from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.repair import FRAGMENT_CAP, repair_file
from coroner.evidence.writer import TAIL_LINE_CAP, Durability, EvidenceWriter, TailRepairRequired

CAP = 1024 * 1024  # fragment copy cap (restated)
LINE_CAP = 2 * 1024 * 1024  # a last line longer than this is never valid (restated, D28)
NOW = "2025-01-01T12:05:00.004Z"
RECV = "2025-01-01T12:00:06.530Z"
PROP = dict(deadline=None, derandomize=True, database=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


def now() -> str:
    return NOW


def test_cap_constants_match_the_approved_contract() -> None:
    assert FRAGMENT_CAP == CAP and TAIL_LINE_CAP == LINE_CAP


# --- independent oracle ---------------------------------------------------------------


def _no_constant(name: str):
    raise ValueError(name)


def valid_object(line: bytes) -> bool:
    if len(line) > LINE_CAP:
        return False
    try:
        value = json.loads(line.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return isinstance(value, dict)


def oracle(original: bytes) -> dict | None:
    """Expected repair facts, or None when nothing needs repair. Pure byte operations."""
    if not original:
        return None
    terminated = original.endswith(b"\n")
    body = original[:-1] if terminated else original
    start = body.rfind(b"\n") + 1
    line = body[start:]
    if terminated and valid_object(line):
        return None
    if terminated:
        reason, action = "invalid_json_last_line", "none_needed_already_terminated"
    else:
        reason = "missing_trailing_newline" if valid_object(line) else "incomplete_last_line"
        action = "appended_newline"
    return {
        "line": line, "line_no": original[:start].count(b"\n") + 1, "fragment_len": len(line),
        "fragment": line[:CAP], "truncated": len(line) > CAP, "reason": reason, "action": action,
    }


def is_clean(data: bytes) -> bool:
    return oracle(data) is None


def sample(seq: int) -> dict:
    body = {
        "run_id": "r1", "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 71303168, "request_count": 9,
        "loop_delay_ms": 0.84, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }
    return {"record_type": "sample", "schema_version": 1, "run_id": "r1", "recv_time": RECV, "sample": body}


def line_of(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"


def sample_lines(count: int) -> bytes:
    return b"".join(line_of(sample(i)) for i in range(count))


def count_valid_samples(original: bytes) -> int:
    """Complete lines (the unterminated last line is not complete) that parse to a sample record of r1."""
    complete = original.split(b"\n")[:-1]
    total = 0
    for raw in complete:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("record_type") == "sample" and value.get("run_id") == "r1":
            total += 1
    return total


# --- IO doubles -------------------------------------------------------------------------


class SpyFile:
    """Wraps a real append handle. Exposes only write/flush/fileno, so any seek, truncate or read raises AttributeError."""

    def __init__(self, real, events: list, limit: int | None, fail_flush: bool) -> None:
        self._real, self._events, self._limit, self._fail_flush = real, events, limit, fail_flush

    def __enter__(self) -> "SpyFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        self._events.append(("write", len(data)))
        if self._limit is None:
            return self._real.write(data)
        self._real.write(data[: self._limit])
        self._real.flush()
        raise OSError("no space left on device")

    def flush(self) -> None:
        self._events.append(("flush",))
        if self._fail_flush:
            raise OSError("flush failed")
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()


class Env:
    """A temp evidence directory, a writer with a recording fsync, and one event log (write, flush, fsync order)."""

    def __init__(self, root: Path, level: Durability = Durability.CRASH, *, fail_fsync: bool = False) -> None:
        self.directory = root / "evidence"
        self.directory.mkdir()
        self.events: list[tuple] = []
        self.modes: list[str] = []
        self.limit: int | None = None
        self.fail_flush = False
        self.fail_fsync = fail_fsync
        self.writer = EvidenceWriter(self.directory, level, fsync=self._fsync, fsync_dir=self._fsync_dir)
        self.path = self.directory / "r1.jsonl"

    def _fsync(self, fd: int) -> None:
        if self.fail_fsync:
            self.events.append(("fsync_fail",))
            raise OSError("fsync failed")
        self.events.append(("fsync_file", os.fstat(fd).st_size))

    def _fsync_dir(self, path: Path) -> None:
        self.events.append(("fsync_dir", Path(path)))

    @contextlib.contextmanager
    def patched(self):
        def opener(p, m):
            self.modes.append(m)
            if m == "ab":
                self.events.append(("open_ab",))
                return SpyFile(builtins.open(p, m), self.events, self.limit, self.fail_flush)
            return builtins.open(p, m)

        with mock.patch.object(writer_module, "open", opener, create=True), mock.patch.object(repair_module, "open", opener, create=True):
            yield


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the full check -----------------------------------------------------------------------


def run_case(original: bytes, level: Durability = Durability.CRASH) -> dict | None:
    """Repair `original`, append one record, read it back, and check every part of Property 8. Returns the oracle."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp), level)
        env.path.write_bytes(original)
        digest = sha(original)
        expected = oracle(original)
        crash = level is Durability.CRASH

        with env.patched():
            record = repair_file(env.path, env.writer, now)
        repaired = env.path.read_bytes()

        # 1, 2: original bytes are an unmodified prefix; no complete line changed, moved or lost
        assert repaired[: len(original)] == original and sha(repaired[: len(original)]) == digest
        assert len(repaired) >= len(original)
        original_parts = original.split(b"\n")
        repaired_parts = repaired.split(b"\n")
        assert repaired_parts[: len(original_parts) - 1] == original_parts[:-1]
        if original_parts[-1]:
            # the fragment line itself is intact, followed by the one-byte D30 marker (an unterminated tail only)
            assert repaired_parts[len(original_parts) - 1] == original_parts[-1] + b"\x00"

        # 6: only the documented opens (read-only inspect, one append-mode write), never a rewrite mode
        assert set(env.modes) <= {"rb", "ab"}

        if expected is None:
            assert record is None and repaired == original  # clean or empty: no change, no I/O, no sync
            assert env.events == [] and "ab" not in env.modes
        else:
            assert record is not None
            appended = repaired[len(original):]
            # 3, 4, 5
            assert record["record_type"] == "repair" and record["run_id"] == "r1" and record["file"] == "r1.jsonl"
            assert record["recv_time"] == NOW and record["schema_version"] == 1
            assert record["line_no"] == expected["line_no"]
            assert record["fragment_len"] == expected["fragment_len"]
            assert record["fragment_truncated"] is expected["truncated"]
            assert base64.b64decode(record["fragment_b64"], validate=True) == expected["fragment"]
            assert record["reason"] == expected["reason"] and record["action"] == expected["action"]
            lead = b"\x00\n" if expected["action"] == "appended_newline" else b""  # D30 marker, then newline
            assert appended.startswith(lead) and appended.endswith(b"\n")
            record_line = appended[len(lead):]
            assert record_line.count(b"\n") == 1 and json.loads(record_line) == record
            # the fragment is still in the file exactly once at its place, followed by a newline
            assert repaired[len(original) - len(expected["line"]) - (1 if original.endswith(b"\n") else 0):][: len(expected["line"])] == expected["line"]
            # 6, 7: one append-mode open and one write of exactly the repair bytes, flushed and (Crash) fsynced after
            assert env.modes.count("ab") == 1
            wanted = [("open_ab",), ("write", len(appended)), ("flush",)]
            if crash:
                wanted.append(("fsync_file", len(repaired)))  # the sync saw the whole repair, and no directory sync (file existed)
            assert env.events == wanted

        # 10, 7: the next append is accepted, starts on a fresh line, and syncs strictly after the repair
        assert repaired == b"" or repaired.endswith(b"\n")
        mark = len(env.events)
        with env.patched():
            env.writer.append("r1", [sample(1000)])
        final = env.path.read_bytes()
        new_line = line_of(sample(1000))
        assert final == repaired + new_line
        tail_events = env.events[mark:]
        assert tail_events[0] == ("open_ab",) and tail_events[-1][0] == ("fsync_file" if crash else "flush")
        assert env.events[:mark] == ([] if expected is None else wanted)  # repair events all precede the append's

        # 11, 12: still readable, fragment quarantined, valid lines and repair record returned, samples counted
        result = read_evidence(env.path)
        summary = summarize(result)
        assert summary.sample_count == count_valid_samples(original) + 1
        if expected is not None:
            assert record in result.records
            quarantined = {e.line_no: e for e in result.errors}
            assert expected["line_no"] in quarantined and quarantined[expected["line_no"]].has_repair_record
            assert expected["line_no"] not in result.line_numbers
        return expected


# --- strategies ------------------------------------------------------------------------------------

SPECIAL = [b"\r", b"\x00", b"\xff", "\u2028".encode(), b'"', b"\\", b"\xc3", b"\xef\xbb\xbf", b"{", b"}", b"\x0b", b" "]
SAMPLE_BODY = line_of(sample(7))[:-1]

arbitrary = st.binary(max_size=4096).map(lambda b: b.replace(b"\n", b"\x0b"))
specials = st.lists(st.sampled_from(SPECIAL), max_size=24).map(b"".join)
truncated_sample = st.integers(0, len(SAMPLE_BODY) - 1).map(lambda n: SAMPLE_BODY[:n])
json_scalars = st.none() | st.booleans() | st.integers(-1000, 1000) | st.text(max_size=20)
json_objects = st.builds(
    lambda d, ascii_only, pad: pad + json.dumps(d, ensure_ascii=ascii_only).encode("utf-8") + pad,
    st.dictionaries(st.text(max_size=8), json_scalars | st.lists(json_scalars, max_size=3), max_size=4),
    st.booleans(),
    st.sampled_from([b"", b" "]),
)
tails = st.one_of(arbitrary, specials, truncated_sample, json_objects, st.just(SAMPLE_BODY), st.just(b""))
prefixes = st.lists(st.one_of(st.none(), arbitrary), max_size=4).map(
    lambda items: b"".join(line_of(sample(i)) if item is None else item + b"\n" for i, item in enumerate(items))
)
originals = st.builds(lambda p, t, nl: p + t + (b"\n" if nl else b""), prefixes, tails, st.booleans())


@settings(max_examples=400, **PROP)
@given(originals)
def test_property_8_repair_preserves_every_original_byte(original: bytes) -> None:
    run_case(original)


def run_interrupted(original: bytes, *, limit: int | None = None, fail_flush: bool = False, fail_fsync: bool = False) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp), Durability.CRASH, fail_fsync=fail_fsync)
        env.path.write_bytes(original)
        digest = sha(original)
        expected = oracle(original)
        env.limit, env.fail_flush = limit, fail_flush
        if expected is None:  # nothing to repair: injected faults are never reached
            with env.patched():
                assert repair_file(env.path, env.writer, now) is None
            assert env.events == [] and env.path.read_bytes() == original
            return
        with env.patched():
            with pytest.raises(OSError):  # 8: the error propagates, no record is returned
                repair_file(env.path, env.writer, now)
        # no durability acknowledgement: nothing is synced after a failed write or flush; a failed fsync is the last event
        if fail_fsync:
            assert env.events[-1] == ("fsync_fail",)
        else:
            assert not any(e[0] == "fsync_file" for e in env.events)
        mid = env.path.read_bytes()
        assert mid[: len(original)] == original and sha(mid[: len(original)]) == digest  # 9: prefix intact
        if limit is not None:
            assert len(mid) - len(original) <= limit
        if not is_clean(mid):
            before = mid
            with pytest.raises(TailRepairRequired):
                EvidenceWriter(env.directory, Durability.PROCESS).append("r1", [sample(1000)])
            assert env.path.read_bytes() == before
        # retry with healthy I/O converges: at most two further repairs, then nothing to do
        healthy = EvidenceWriter(env.directory, Durability.CRASH, fsync=lambda fd: None, fsync_dir=lambda p: None)
        outcomes = [repair_file(env.path, healthy, now) for _ in range(3)]
        assert outcomes[-1] is None and sum(o is not None for o in outcomes) <= 2
        final = env.path.read_bytes()
        assert final[: len(original)] == original and sha(final[: len(original)]) == digest
        assert is_clean(final) and len(final) >= len(mid)
        # D30: at no interruption point, and after no retry, is the unterminated original line accepted
        acknowledged = count_valid_samples(original)
        assert summarize(read_evidence(mid, name="r1.jsonl")).sample_count == acknowledged
        assert summarize(read_evidence(final, name="r1.jsonl")).sample_count == acknowledged
        if expected["action"] == "appended_newline":
            # the physical line holds the original fragment, then one or two NUL markers (two after a marker-only cut)
            physical = final[len(original) - len(expected["line"]):].split(b"\n", 1)[0]
            assert physical in (expected["line"] + b"\x00", expected["line"] + b"\x00\x00")
        healthy.append("r1", [sample(1000)])
        assert env.path.read_bytes() == final + line_of(sample(1000))  # next record on a fresh line


small_tails = st.one_of(
    st.binary(max_size=60).map(lambda b: b.replace(b"\n", b"\x0b")), truncated_sample,
    st.just(SAMPLE_BODY), st.just(b'{"a":1}'), specials,
)
small_originals = st.builds(
    lambda n, t, nl: sample_lines(n) + t + (b"\n" if nl else b""), st.integers(0, 2), small_tails, st.booleans()
)


@settings(max_examples=250, **PROP)
@given(small_originals, st.integers(0, 700))
def test_property_8_interrupted_repair_keeps_the_prefix_and_retries_converge(original: bytes, cut: int) -> None:
    run_interrupted(original, limit=cut)


# --- explicit examples -------------------------------------------------------------------------------

SPECIAL_EXAMPLES = {
    "empty": b"",
    "clean": sample_lines(3),
    "blank": b"\n",
    "partial": sample_lines(2) + SAMPLE_BODY[:40],
    "objnl": sample_lines(1) + b'{"a":1}',
    "sample-nonl": sample_lines(2) + SAMPLE_BODY,
    "utf8-bad": sample_lines(1) + b'{"a":"\xff\xfe"}',
    "utf8-bad-nl": sample_lines(1) + b"\xff\xfe\x00junk\n",
    "term-bad": sample_lines(2) + b'{"truncated":\n',
    "term-array": sample_lines(1) + b"[1,2,3]\n",
    "nan": sample_lines(1) + b'{"a":NaN}',
    "damaged-mid": sample_lines(1) + b"garbage\n" + sample_lines(2) + b'{"half',
    "specials": sample_lines(1) + b'\r\x00\xff\xe2\x80\xa8"\\{',
    "crlf": sample_lines(1) + b'{"a":1}\r',
    "no-nl-file": b"x",
}


@pytest.mark.parametrize("name", sorted(SPECIAL_EXAMPLES))
def test_property_8_explicit_tails(name: str) -> None:
    expected = run_case(SPECIAL_EXAMPLES[name])
    assert (expected is None) == (name in {"empty", "clean"})


def big_object(size: int) -> bytes:
    return b'{"k":"' + b"v" * (size - 8) + b'"}'


BIG = {
    "cap-1": (b"q" * (CAP - 1), False, "incomplete_last_line", False),
    "cap": (b"q" * CAP, False, "incomplete_last_line", False),
    "cap+1": (b"q" * (CAP + 1), False, "incomplete_last_line", True),
    "2m": (b"q" * LINE_CAP, False, "incomplete_last_line", True),
    "2m+1": (b"q" * (LINE_CAP + 1), False, "incomplete_last_line", True),
    "obj-2m": (big_object(LINE_CAP), False, "missing_trailing_newline", True),
    "obj-2m+1": (big_object(LINE_CAP + 1), False, "incomplete_last_line", True),
    "t-2m+1": (big_object(LINE_CAP + 1), True, "invalid_json_last_line", True),
    "t-cap+1": (b"q" * (CAP + 1), True, "invalid_json_last_line", True),
}


@pytest.mark.parametrize("name", sorted(BIG))
def test_property_8_fragments_around_the_copy_and_parse_caps(name: str) -> None:
    fragment, terminated, reason, truncated = BIG[name]
    original = sample_lines(1) + fragment + (b"\n" if terminated else b"")
    expected = run_case(original)
    assert expected is not None and expected["reason"] == reason and expected["truncated"] is truncated
    assert expected["fragment_len"] == len(fragment)


def test_property_8_a_terminated_object_of_exactly_the_line_cap_is_valid_and_untouched() -> None:
    assert run_case(sample_lines(1) + big_object(LINE_CAP) + b"\n") is None


# 3 fixed cuts for each large fragment, plus the byte that separates "newline only" from "newline and some record"
@pytest.mark.parametrize("cut", [0, 1, 2, 500, 100000])
@pytest.mark.parametrize("name", ["cap", "cap+1", "obj-2m"])
def test_property_8_interrupted_large_repairs_converge(name: str, cut: int) -> None:
    fragment, terminated, _, _ = BIG[name]
    run_interrupted(sample_lines(1) + fragment + (b"\n" if terminated else b""), limit=cut)


@pytest.mark.parametrize("name", ["partial", "sample-nonl", "term-bad", "utf8-bad"])
@pytest.mark.parametrize("fault", ["flush", "fsync"])
def test_property_8_flush_and_fsync_failures_propagate_and_lose_nothing(name: str, fault: str) -> None:
    run_interrupted(SPECIAL_EXAMPLES[name], fail_flush=fault == "flush", fail_fsync=fault == "fsync")


@pytest.mark.parametrize("name", ["partial", "term-bad"])
def test_property_8_process_durability_repair_writes_and_flushes_without_fsync(name: str) -> None:
    run_case(SPECIAL_EXAMPLES[name], Durability.PROCESS)


@pytest.mark.parametrize("cut", [1, 2], ids=["marker", "marker-nl"])
@pytest.mark.parametrize("tail", ["obj", "sample"])
def test_property_8_valid_object_cut_after_the_marker_is_not_clean_and_never_counted(tail: str, cut: int) -> None:
    """D30 closes the former limitation (newline-only interruption of a valid JSON fragment looked clean)."""
    original = sample_lines(1) + (b'{"a":1}' if tail == "obj" else SAMPLE_BODY)
    run_interrupted(original, limit=cut)
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        env.path.write_bytes(original)
        env.limit = cut
        with env.patched(), pytest.raises(OSError):
            repair_file(env.path, env.writer, now)
        assert env.path.read_bytes() == original + b"\x00\n"[:cut]
        assert not is_clean(env.path.read_bytes())
        assert summarize(read_evidence(env.path)).sample_count == 1
