"""Property 9: repair is idempotent and keeps the file readable (task 2.16).

For any evidence file, repairing it twice yields the same file as repairing it once, repairing a clean file
changes nothing, the isolated fragment bytes stay in the file, and reading the repaired file reports the fragment
as exactly one Malformed_Line (file, line number, byte length), excludes it from the Sample count, and returns
every complete record.

**Validates: Requirements 5.5, 5.10, 13.13**

Oracle: `Case` below describes a file by construction (a list of lines, each tagged "is a valid record" or not,
plus a tail). Everything expected (needs repair, line numbers, error kinds, byte lengths, valid records, Sample
count) is computed from those bytes with `bytes`, `json` and the tags. Nothing calls `repair_file`, `inspect_tail`
or a writer helper to compute an expectation. Property 8 (test_prop_repair_bytes.py) owns the byte-level repair
record details; here the record is only checked lightly, and Property 8's `oracle` is used once as a
cross-check of "does this file need repair".

Scope: idempotence is proved after a SUCCESSFUL repair. An interrupted or failed repair may need more than one
further repair before the file is stable (see the interrupted-repair tests). Those tests also pin two
limitations, which Task 3.2 (durable startup recovery) owns:
* a repair that fails after writing the isolating newline for a complete valid Sample leaves a file that looks
  clean, and the reader counts that Sample, because no repair record names it (D27);
* the same holds when more than the newline landed (a partial repair record): the Sample line is terminated and
  valid, and the later repair record names the partial record line, not the Sample line.
A clean-looking tail after a failed fsync is NOT proof of durability.
"""
from __future__ import annotations

import base64
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coroner.evidence.reader import read_evidence, summarize
from coroner.evidence.repair import repair_file
from coroner.evidence.writer import Durability, EvidenceWriter
from tests.unit.evidence.test_prop_repair_bytes import (
    CAP,
    LINE_CAP,
    NOW,
    PROP,
    SAMPLE_BODY,
    Env,
    arbitrary,
    big_object,
    json_objects,
    line_of,
    oracle,
    sample,
    sample_lines,
    sha,
    specials,
    truncated_sample,
)
from tests.unit.evidence.test_prop_repair_bytes import now as now_fn

NAME = "r1.jsonl"


# --- independent model ---------------------------------------------------------------------------


def _no_constant(name: str):
    raise ValueError(name)


def classify(raw: bytes) -> str:
    """Reader kind of a terminated line that is not a valid record: utf8, then JSON, then object, then record."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid_utf8"
    try:
        value = json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return "invalid_json"
    return "invalid_record" if isinstance(value, dict) else "not_an_object"


def is_json_object(raw: bytes) -> bool:
    """What repair treats as a valid last line: a UTF-8 JSON object of at most 2 MiB."""
    return len(raw) <= LINE_CAP and classify(raw) == "invalid_record"


def parse(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def valid_line(seq: int, reason: str, ascii_only: bool, with_null: bool) -> bytes:
    record = sample(seq)
    if with_null:  # free-form Unicode text lives only in null_reasons
        record["sample"]["rss_bytes"] = None
        record["sample"]["null_reasons"] = {"rss_bytes": reason}
    return json.dumps(record, ensure_ascii=ascii_only, allow_nan=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Case:
    items: tuple  # complete lines before the tail: (bytes without newline, is_valid_record)
    tail: bytes = b""
    terminated: bool = False
    tail_ok: bool = False  # the tail is a valid record (only meaningful when it is a Sample line)

    @property
    def original(self) -> bytes:
        return b"".join(b + b"\n" for b, _ in self.items) + self.tail + (b"\n" if self.terminated else b"")

    @property
    def has_tail(self) -> bool:
        return bool(self.tail) or self.terminated

    @property
    def tail_line_no(self) -> int:
        return len(self.items) + 1

    @property
    def needs_repair(self) -> bool:
        if not self.has_tail:
            return False
        return (not self.terminated) or not is_json_object(self.tail)

    def expected_errors(self) -> list[tuple]:
        errors = [(NAME, i + 1, len(b), classify(b), False) for i, (b, ok) in enumerate(self.items) if not ok]
        if self.has_tail:
            n, size = self.tail_line_no, len(self.tail)
            if not self.terminated:  # the physical line is the fragment plus the one-byte D30 marker
                errors.append((NAME, n, size + 1, "isolated_by_repair", True))
            elif not self.tail_ok:
                errors.append((NAME, n, size, classify(self.tail), self.needs_repair))
        return errors

    def expected_valid(self) -> list[tuple[int, dict]]:
        found = [(i + 1, parse(b)) for i, (b, ok) in enumerate(self.items) if ok]
        if self.has_tail and self.terminated and self.tail_ok:
            found.append((self.tail_line_no, parse(self.tail)))
        return found


def normalised(case: Case) -> Case:
    """With no tail bytes and no newline, the file ends after its last complete line, which is then the tail."""
    if case.has_tail or not case.items:
        return case
    (last, ok), rest = case.items[-1], case.items[:-1]
    return Case(rest, last, True, ok)


# --- the property check ----------------------------------------------------------------------------


def check_case(case: Case, level: Durability = Durability.CRASH) -> dict | None:
    """Repair once, twice, three times, read, and check every part of Property 9. Returns the first repair record."""
    case = normalised(case)
    original = case.original
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp), level)
        env.path.write_bytes(original)
        first = repair_file(env.path, env.writer, now_fn)
        once = env.path.read_bytes()

        # the repair decision agrees with the independent model and with Property 8's oracle
        assert case.needs_repair == (oracle(original) is not None)

        if not case.needs_repair:  # clean file (or empty): returns None, no record, no byte changes, no I/O
            assert first is None and once == original and env.events == []
        else:
            assert first is not None and first["record_type"] == "repair"
            assert first["file"] == NAME and first["run_id"] == "r1" and first["recv_time"] == NOW
            assert first["line_no"] == case.tail_line_no and first["fragment_len"] == len(case.tail)
            lead = b"" if case.terminated else b"\x00\n"  # D30: NUL marker, then the isolating newline
            assert once.startswith(original) and once[len(original):].startswith(lead)  # append-only
            record_line = once[len(original) + len(lead):]
            assert record_line.endswith(b"\n") and record_line.count(b"\n") == 1 and parse(record_line) == first
            # the damaged fragment is still in the file at its own line, byte for byte (plus the marker if unterminated)
            assert once.split(b"\n")[case.tail_line_no - 1] == case.tail + (b"" if case.terminated else b"\x00")
            assert not first["fragment_truncated"] or len(case.tail) > CAP

        # a second and a third repair change nothing and return None
        for _ in range(2):
            assert repair_file(env.path, env.writer, now_fn) is None
            assert env.path.read_bytes() == once

        # reading: never modifies the file, is repeatable, and reports exactly the expected lines
        digest = sha(once)
        result = read_evidence(env.path)
        again = read_evidence(env.path)
        assert sha(env.path.read_bytes()) == digest
        assert result == again

        got = [(e.file, e.line_no, e.byte_length, e.kind, e.has_repair_record) for e in result.errors]
        assert got == case.expected_errors()  # pre-existing bad lines plus exactly one for a newly isolated fragment

        valid = case.expected_valid()
        expected_numbers = [n for n, _ in valid] + ([case.tail_line_no + (1 if case.has_tail else 0)] if first else [])
        expected_records = [r for _, r in valid] + ([first] if first else [])
        assert result.records == expected_records and result.line_numbers == expected_numbers

        summary = summarize(result)
        assert summary.sample_count == len(valid)  # the isolated fragment and the repair record are not Samples
        assert summary.malformed_line_count == len(got)
        return first


# --- strategies ----------------------------------------------------------------------------------------

reasons = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=60)
valid_items = st.builds(lambda s, r, a, n: (valid_line(s, r, a, n), True), st.integers(0, 10**6), reasons, st.booleans(), st.booleans())
junk_bytes = st.one_of(
    arbitrary, specials, truncated_sample, json_objects,
    st.sampled_from([b"", b"[1,2]", b"NaN", b'{"a":"\xff\xfe"}', b"\xff\xfe\x00junk", b'{"a":NaN}', b"\r"]),
)
items_st = st.lists(st.one_of(valid_items, junk_bytes.map(lambda b: (b, False))), max_size=5).map(tuple)
tails_st = st.one_of(
    st.just((b"", False, False)),
    st.builds(lambda item, term: (item[0], term, True), valid_items, st.booleans()),
    st.builds(lambda b, term: (b, term, False), junk_bytes, st.booleans()),
)
cases = st.builds(lambda items, t: Case(items, *t), items_st, tails_st)
levels = st.sampled_from([Durability.CRASH, Durability.PROCESS])


@settings(max_examples=400, **PROP)
@given(cases, levels)
def test_property_9_repair_is_idempotent_and_the_file_stays_readable(case: Case, level: Durability) -> None:
    check_case(case, level)


clean_files = st.builds(
    lambda lines: b"".join(line + b"\n" for line in lines),
    st.lists(st.one_of(valid_items.map(lambda i: i[0]), json_objects.filter(is_json_object)), max_size=6),
)


@settings(max_examples=150, **PROP)
@given(clean_files, levels)
def test_property_9_a_clean_file_is_never_repaired(original: bytes, level: Durability) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp), level)
        env.path.write_bytes(original)
        for _ in range(2):
            assert repair_file(env.path, env.writer, now_fn) is None
            assert env.path.read_bytes() == original
        assert env.events == []  # no append, no fsync


# --- explicit boundaries -----------------------------------------------------------------------------------

GOOD = (valid_line(0, "x", True, False), True)


def case_of(tail: bytes, terminated: bool, tail_ok: bool = False, lead: int = 1) -> Case:
    return Case(tuple(GOOD for _ in range(lead)), tail, terminated, tail_ok)


def test_zero_byte_file_is_clean() -> None:
    assert check_case(Case(())) is None


def test_file_ending_with_a_valid_newline_terminated_line_is_clean() -> None:
    assert check_case(case_of(valid_line(1, "x", True, False), True, True, lead=3)) is None


def test_terminated_invalid_final_line_is_repaired_once_with_its_own_kind() -> None:
    record = check_case(case_of(b'{"truncated":', True))
    assert record is not None and record["action"] == "none_needed_already_terminated"


def test_a_valid_unterminated_sample_is_isolated_and_not_counted() -> None:
    record = check_case(case_of(valid_line(1, "x", True, False), False, True))
    assert record is not None and record["reason"] == "missing_trailing_newline"


@pytest.mark.parametrize("size", [CAP - 1, CAP, CAP + 1], ids=["cap-1", "cap", "cap+1"])
def test_fragment_copy_boundary(size: int) -> None:
    record = check_case(case_of(b"q" * size, False))
    assert record is not None and record["fragment_len"] == size and record["fragment_truncated"] is (size > CAP)


def test_a_terminated_valid_object_of_exactly_two_mebibytes_is_clean() -> None:
    # exactly LINE_CAP: valid JSON object, so nothing to repair (the reader still rejects it as a record)
    assert check_case(case_of(big_object(LINE_CAP), True)) is None


@pytest.mark.parametrize("terminated", [False, True], ids=["unterm", "term"])
def test_a_valid_object_of_two_mebibytes_plus_one_is_repaired_once_then_stable(terminated: bool) -> None:
    record = check_case(case_of(big_object(LINE_CAP + 1), terminated))
    assert record is not None and record["fragment_truncated"]
    assert record["action"] == ("none_needed_already_terminated" if terminated else "appended_newline")


@pytest.mark.parametrize("terminated", [False, True], ids=["unterm", "term"])
def test_fragment_over_two_mebibytes_is_repaired_once_and_the_big_record_is_clean(terminated: bool) -> None:
    fragment = b"q" * (LINE_CAP + 5)
    record = check_case(case_of(fragment, terminated))
    assert record is not None and record["fragment_len"] == len(fragment) and record["fragment_truncated"]
    # the repair record carries 1 MiB of fragment, so it is bigger than 1 MiB and smaller than 2 MiB
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        env.path.write_bytes(case_of(fragment, terminated).original)
        repair_file(env.path, env.writer, now_fn)
        record_line = env.path.read_bytes().rsplit(b"\n", 2)[-2]
        assert CAP < len(record_line) < LINE_CAP
        assert repair_file(env.path, env.writer, now_fn) is None


# --- interrupted and failed repairs -----------------------------------------------------------------------

PARTIAL = b'{"record_type":"sample","sche'
FRAGMENTS = {"partial": PARTIAL, "badutf8": b'{"a":"\xff', "validsample": SAMPLE_BODY}
BASE = sample_lines(2)


def repair_length(original: bytes) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        env.path.write_bytes(original)
        repair_file(env.path, env.writer, now_fn)
        return len(env.path.read_bytes()) - len(original)


def stabilise(path: Path, directory: Path) -> tuple[list, bytes]:
    healthy = EvidenceWriter(directory, Durability.CRASH, fsync=lambda fd: None, fsync_dir=lambda p: None)
    outcomes = []
    for _ in range(4):
        outcomes.append(repair_file(path, healthy, now_fn))
        if outcomes[-1] is None:
            break
    stable = path.read_bytes()
    for _ in range(2):  # once stable, it stays stable
        assert repair_file(path, healthy, now_fn) is None and path.read_bytes() == stable
    return outcomes, stable


@pytest.mark.parametrize("name", sorted(FRAGMENTS))
@pytest.mark.parametrize("where", ["0", "1", "2", "mid", "len-2", "len-1"])
def test_a_partially_written_repair_converges_to_a_stable_readable_file(name: str, where: str) -> None:
    original = BASE + FRAGMENTS[name]
    total = repair_length(original)
    cut = {"0": 0, "1": 1, "2": 2, "mid": total // 2, "len-2": total - 2, "len-1": total - 1}[where]
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        env.path.write_bytes(original)
        env.limit = cut
        with env.patched(), pytest.raises(OSError):
            repair_file(env.path, env.writer, now_fn)
        interrupted = env.path.read_bytes()
        assert interrupted[: len(original)] == original and len(interrupted) - len(original) == cut

        outcomes, stable = stabilise(env.path, env.directory)
        assert outcomes[-1] is None and sum(o is not None for o in outcomes) <= 2  # converges; extra records allowed
        assert stable.startswith(interrupted)  # append-only, nothing lost
        # the damaged fragment is still at its line, then one marker (two after a marker-only cut and its retry)
        assert stable.split(b"\n")[2] in (FRAGMENTS[name] + b"\x00", FRAGMENTS[name] + b"\x00\x00")
        assert interrupted[: len(original)] == original

        result = read_evidence(env.path)
        got = [r for r in result.records if r["record_type"] == "sample"]
        expected = [parse(line) for line in BASE.split(b"\n")[:-1]]
        # D30: the marker is the first byte written, so at no cut is the never-acknowledged Sample counted
        assert got == expected and summarize(result).sample_count == len(expected)
        assert all(e.line_no != 1 and e.line_no != 2 for e in result.errors)  # the two complete lines are never errors
        assert any(r["record_type"] == "repair" for r in result.records)


@pytest.mark.parametrize("cut", [1, 2], ids=["marker", "marker-nl"])
def test_a_valid_sample_cut_after_the_marker_is_never_counted_and_one_retry_converges(cut: int) -> None:
    """D30 closes the former newline-only limitation: the marker makes the Sample line invalid JSON at once."""
    original = BASE + SAMPLE_BODY
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp))
        env.path.write_bytes(original)
        env.limit = cut
        with env.patched(), pytest.raises(OSError):
            repair_file(env.path, env.writer, now_fn)
        assert env.path.read_bytes() == original + b"\x00\n"[:cut]
        assert summarize(read_evidence(env.path)).sample_count == 2
        record = repair_file(env.path, env.writer, now_fn)
        assert record is not None and record["line_no"] == 3
        assert base64.b64decode(record["fragment_b64"]) == SAMPLE_BODY + b"\x00"  # the earlier marker is included
        assert repair_file(env.path, env.writer, now_fn) is None
        result = read_evidence(env.path)
        kind = "isolated_by_repair" if cut == 1 else "invalid_json"
        assert [(e.line_no, e.byte_length, e.kind, e.has_repair_record) for e in result.errors] == [
            (3, len(SAMPLE_BODY) + {1: 2, 2: 1}[cut], kind, True)]  # two markers after a marker-only cut
        assert summarize(result).sample_count == 2


def test_a_failed_fsync_leaves_a_complete_looking_repair_that_a_second_repair_does_not_touch() -> None:
    original = BASE + PARTIAL
    with tempfile.TemporaryDirectory() as tmp:
        env = Env(Path(tmp), Durability.CRASH, fail_fsync=True)
        env.path.write_bytes(original)
        with env.patched(), pytest.raises(OSError):  # no record is returned
            repair_file(env.path, env.writer, now_fn)
        written = env.path.read_bytes()
        assert written.startswith(original + b"\x00\n") and written.endswith(b"\n")  # the whole repair reached the file
        healthy = EvidenceWriter(env.directory, Durability.CRASH, fsync=lambda fd: None, fsync_dir=lambda p: None)
        # The tail now looks clean, so repair has nothing to do. That is NOT proof the bytes are durable: the
        # fsync failed. Durable startup recovery belongs to Task 3.2.
        assert repair_file(env.path, healthy, now_fn) is None and env.path.read_bytes() == written
        result = read_evidence(env.path)
        assert [(e.line_no, e.kind, e.has_repair_record) for e in result.errors] == [(3, "isolated_by_repair", True)]
        assert summarize(result).sample_count == 2
