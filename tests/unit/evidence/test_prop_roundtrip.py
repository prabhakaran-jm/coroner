"""Property 1: Record framing and round trip (requirements 4.6, 5.4, 5.9, 12.2, 12.3, 13.2).
*For any* list of 0 to 1,000 JSON-serializable records (including strings with newlines, U+2028, and
non-ASCII), serializing each with the Evidence_Writer yields exactly one UTF-8 line terminated by a single
`\\n` with no other raw newline, and reading the written file with the Evidence_Reader returns the same
number of records, in the same order, with every field equal to the original.
**Validates: Requirements 4.6, 5.4, 5.9, 12.2, 12.3, 13.2**

Generated records are schema-valid and JSON-native (dict, list, str, int, float that is finite, bool, None).
Free-form text goes where the schema accepts it: `sample.null_reasons` values (1 to 200 characters) and the
unvalidated payload of the non-sample record types (task 2.19 will validate those payloads; the envelope is
validated now). Lone surrogates are left out of generation: see `test_adjacent_surrogates_do_not_round_trip`.
"""
from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from coroner.evidence.reader import ReadResult, read_evidence
from coroner.evidence.schema import validate_record
from coroner.evidence.writer import Durability, EvidenceWriter, serialize_record

RUN_ID = "run-1"
FILE_NAME = RUN_ID + ".jsonl"
RECV = "2025-01-01T12:00:06.530Z"
TS_FORMAT = "2025-01-01T12:00:{:02d}.{:03d}Z"

# Characters that matter for framing: newlines of every kind, quotes, backslashes, controls, non-ASCII, astral.
NASTY = ["\n", "\r", "\r\n", "\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x00", "\x1f", "\x7f", '"', "\\", "/", "\\n", "\\u2028",
         "é", "ß", "日本語", "\U0001f600", "\U0010ffff", "\ufeff", "\u200b", "\ufffd"]
CHARS = st.characters(blacklist_categories=("Cs",))  # no surrogates
TEXT = st.one_of(
    st.text(CHARS, max_size=40),
    st.lists(st.one_of(st.sampled_from(NASTY), st.text(CHARS, max_size=5)), max_size=12).map("".join),
)
REASON = TEXT.filter(lambda s: 1 <= len(s) <= 200)| st.sampled_from(NASTY)
BIG_INT = st.integers(-(2**80), 2**80)
FINITE = st.floats(allow_nan=False, allow_infinity=False, width=64)
JSON_LEAF = st.one_of(st.none(), st.booleans(), BIG_INT, FINITE, TEXT)
JSON_VALUE = st.recursive(
    JSON_LEAF,
    lambda inner: st.one_of(st.lists(inner, max_size=4), st.dictionaries(TEXT, inner, max_size=4)),
    max_leaves=12,
)

NULLABLE = ("rss_bytes", "request_count", "loop_delay_ms", "container_id")


@st.composite
def sample_bodies(draw) -> dict:
    """A schema-valid Sample (without seq and run_id; the caller sets them)."""
    body: dict = {
        "pid": draw(st.integers(-(2**63), 2**63 - 1)),
        "container_id_source": draw(st.sampled_from(["mountinfo", "cgroup", "hostname", "none"])),
        "interval_ms": draw(st.integers(100, 60000)),
        "dropped": draw(st.integers(0, 4_294_967_295)),
        "ts": TS_FORMAT.format(draw(st.integers(0, 59)), draw(st.integers(0, 999))),
    }
    reasons: dict = {}
    for field in NULLABLE:
        if draw(st.booleans()):
            body[field] = None
            reasons[field] = draw(REASON)
        elif field in ("rss_bytes", "request_count"):
            body[field] = draw(st.integers(0, 2**63))
        elif field == "loop_delay_ms":
            body[field] = draw(st.one_of(st.integers(0, 10**6), st.floats(0, 1e9, allow_nan=False, allow_infinity=False)))
        else:
            body[field] = draw(st.sampled_from(["a" * 12, "0123456789abcdef" * 4]))
    if reasons or draw(st.booleans()):
        body["null_reasons"] = reasons
    return body


@st.composite
def other_bodies(draw) -> tuple[str, dict]:
    """A non-sample record type and its (not yet validated) payload under `detail`. `repair` is left out: it links lines."""
    record_type = draw(st.sampled_from(["run_metadata", "gap", "sequence_anomaly", "rejection", "termination"]))
    return record_type, {"detail": draw(JSON_VALUE), "label": draw(TEXT)}


@st.composite
def record_lists(draw) -> list[dict]:
    """0 to 1,000 distinct records. A small pool of drawn bodies is cycled and stamped with its index, so that order matters."""
    n = draw(st.one_of(st.integers(0, 1000), st.sampled_from([0, 1, 2, 999, 1000])))
    if n == 0:
        return []
    pool = draw(st.lists(st.one_of(st.tuples(st.just("sample"), sample_bodies()), other_bodies()), min_size=1, max_size=12))
    return [build_record(i, *pool[i % len(pool)]) for i in range(n)]


def build_record(index: int, record_type: str, body: dict) -> dict:
    if record_type == "sample":
        sample = {"run_id": RUN_ID, "seq": index, **body}
        return {"record_type": "sample", "schema_version": 1, "run_id": RUN_ID, "recv_time": RECV, "sample": sample}
    return {"record_type": record_type, "schema_version": 1, "run_id": RUN_ID, "recv_time": RECV, "index": index, **body}


class FsyncDouble:
    def __init__(self) -> None:
        self.files = 0
        self.dirs = 0

    def fsync(self, fd: int) -> None:
        self.files += 1

    def fsync_dir(self, path: Path) -> None:
        self.dirs += 1


def strict_equal(a: object, b: object) -> bool:
    """Equality that also requires the same Python type (so 1, 1.0 and True differ) and the same key order."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return a == b and str(a) == str(b)  # distinguishes 0.0 from -0.0
    return a == b


def check_round_trip(records: list[dict], expected_bytes: bytes, data: bytes, result: ReadResult) -> None:
    """Raise AssertionError unless `result` is a clean, exact round trip of `records`."""
    assert data == expected_bytes, "file bytes differ from the concatenated serialized records"
    assert result.errors == [], f"quarantined lines: {result.errors[:2]}"
    assert len(result.records) == len(records), f"{len(result.records)} records read, {len(records)} written"
    assert result.line_numbers == list(range(1, len(records) + 1))
    for index, (want, got) in enumerate(zip(records, result.records)):
        assert strict_equal(want, got), f"record {index} differs after the round trip"


def check_framing(records: list[dict]) -> bytes:
    """Each serialized record is one ASCII line ending in a single newline; return their concatenation."""
    out: list[bytes] = []
    for record in records:
        line = serialize_record(record)
        assert line.endswith(b"\n") and line.count(b"\n") == 1, "not exactly one newline, at the end"
        body = line[:-1]
        assert b"\r" not in body and body.isascii()  # JSON escaping: U+2028, U+2029 and every non-ASCII character are \\u escapes
        assert len(line.decode("utf-8").splitlines()) == 1  # not even a Unicode line separator splits it
        assert isinstance(json.loads(body), dict)
        out.append(line)
    return b"".join(out)


MAX_BATCHES = 20


def write_and_read(records: list[dict], chunk: int) -> tuple[bytes, bytes, ReadResult, FsyncDouble, bool]:
    double = FsyncDouble()
    with tempfile.TemporaryDirectory(prefix="coroner-p1-") as tmp:
        writer = EvidenceWriter(Path(tmp), Durability.CRASH, fsync=double.fsync, fsync_dir=double.fsync_dir)
        expected = check_framing(records)
        chunk = max(chunk, -(-len(records) // MAX_BATCHES))  # at most MAX_BATCHES appends per example, to bound the file opens
        for start in range(0, len(records), chunk):
            writer.append(RUN_ID, records[start : start + chunk])
        path = writer.path_for(RUN_ID)
        existed = path.exists()
        if not existed:
            return expected, b"", read_evidence(b"", name=FILE_NAME), double, existed
        data = path.read_bytes()
        return expected, data, read_evidence(path), double, existed


SETTINGS = settings(
    max_examples=100, deadline=None, derandomize=True, database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.large_base_example],
)


@SETTINGS
@given(record_lists(), st.integers(1, 1000))
@example([], 1)
def test_property_1_framing_and_round_trip(records: list[dict], chunk: int) -> None:
    for record in records:
        assert validate_record(record) == []  # the generator really produces schema-valid records
    expected, data, result, double, existed = write_and_read(records, chunk)
    check_round_trip(records, expected, data, result)
    assert existed is bool(records)  # an empty batch creates no file
    batches = -(-len(records) // max(chunk, -(-len(records) // MAX_BATCHES)))
    assert double.files == batches and double.dirs == (1 if records else 0)  # the fsync double saw every batch


def test_the_empty_list_writes_no_file_and_reads_as_nothing(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path, Durability.CRASH, fsync=FsyncDouble().fsync, fsync_dir=FsyncDouble().fsync_dir)
    assert writer.append(RUN_ID, []).record_count == 0 and not writer.path_for(RUN_ID).exists()
    with pytest.raises(FileNotFoundError):
        read_evidence(writer.path_for(RUN_ID))
    empty = tmp_path / FILE_NAME
    empty.write_bytes(b"")
    result = read_evidence(empty)
    assert result.records == [] and result.errors == [] and result.line_numbers == []


def test_a_batch_of_exactly_1000_records_with_awkward_text_round_trips() -> None:
    awkward = "".join(NASTY)
    records = [
        build_record(i, "sample", {"pid": i, "container_id_source": "none", "interval_ms": 500, "dropped": 0, "ts": TS_FORMAT.format(i % 60, i % 1000),
                                   "rss_bytes": None, "request_count": 1, "loop_delay_ms": 0.0, "container_id": None,
                                   "null_reasons": {"rss_bytes": awkward, "container_id": awkward[::-1]}})
        if i % 2 else build_record(i, "termination", {"detail": {awkward: [awkward, -0.0, 1e300, 2**70, True, None]}, "label": awkward})
        for i in range(1000)
    ]
    assert len(records) == 1000 and all(validate_record(r) == [] for r in records)
    expected, data, result, _, _ = write_and_read(records, 7)
    check_round_trip(records, expected, data, result)
    assert data.count(b"\n") == 1000 and data.isascii()


# --- negative controls: a damaged file must not pass as a round trip -----------------------------------------------


@pytest.mark.parametrize("damage", ["garbage_line", "cut_last_newline", "drop_a_line", "swap_lines"])
def test_a_damaged_file_is_not_accepted_as_a_round_trip(damage: str) -> None:
    records = [build_record(i, "sample", {"pid": i, "container_id_source": "none", "interval_ms": 500, "dropped": 0, "ts": TS_FORMAT.format(i, 0),
                                          "rss_bytes": 1, "request_count": 1, "loop_delay_ms": 1, "container_id": None,
                                          "null_reasons": {"container_id": "n/a"}}) for i in range(4)]
    expected = check_framing(records)
    lines = expected.splitlines(keepends=True)
    if damage == "garbage_line":
        data = lines[0] + b"not json\n" + b"".join(lines[1:])
    elif damage == "cut_last_newline":
        data = expected[:-1]
    elif damage == "drop_a_line":
        data = b"".join(lines[:1] + lines[2:])
    else:
        data = b"".join([lines[1], lines[0], *lines[2:]])
    result = read_evidence(data, name=FILE_NAME)
    with pytest.raises(AssertionError):
        check_round_trip(records, expected, data, result)
    if damage in ("garbage_line", "cut_last_newline"):
        assert result.errors  # quarantined, never counted as a record
        assert len(result.records) < len(records) + 1


# --- signed zero (task 2.14 preparatory check): 0.0 == -0.0 and both are floats, so only the sign bit tells them apart ---


def zero_sign(x: object) -> float:
    assert isinstance(x, float)
    return math.copysign(1.0, x)


def test_strict_equal_tells_signed_zeros_apart_and_the_helper_is_not_fooled_by_ordinary_equality() -> None:
    assert 0.0 == -0.0 and type(0.0) is type(-0.0)  # why == and a type check are not enough
    assert not strict_equal(0.0, -0.0) and not strict_equal(-0.0, 0.0)
    assert strict_equal(-0.0, -0.0) and strict_equal(0.0, 0.0)
    assert not strict_equal({"a": [0.0]}, {"a": [-0.0]})  # also nested
    assert zero_sign(-0.0) == -1.0 and zero_sign(0.0) == 1.0


@pytest.mark.parametrize("where", ["sample", "payload"])
def test_negative_zero_round_trips_as_negative_zero_through_the_writer_and_reader(where: str) -> None:
    if where == "sample":
        body = {"pid": 1, "container_id_source": "none", "interval_ms": 500, "dropped": 0, "ts": TS_FORMAT.format(1, 1), "rss_bytes": 1,
                "request_count": 1, "loop_delay_ms": -0.0, "container_id": None, "null_reasons": {"container_id": "n/a"}}
        record = build_record(0, "sample", body)
        extract = lambda r: r["sample"]["loop_delay_ms"]  # noqa: E731
    else:
        record = build_record(0, "termination", {"detail": {"x": [-0.0, 0.0]}, "label": "z"})
        extract = lambda r: r["detail"]["x"][0]  # noqa: E731
    assert validate_record(record) == [] and zero_sign(extract(record)) == -1.0
    expected, data, result, _, _ = write_and_read([record], 1)
    assert b"-0.0" in data  # the sign is in the file, not only in memory
    check_round_trip([record], expected, data, result)
    assert zero_sign(extract(result.records[0])) == -1.0
    # negative control: the same record with +0.0 is NOT accepted as a round trip of the -0.0 record
    positive = json.loads(json.dumps(record))
    if where == "sample":
        positive["sample"]["loop_delay_ms"] = 0.0
    else:
        positive["detail"]["x"][0] = 0.0
    assert positive == record  # ordinary equality cannot see the difference
    with pytest.raises(AssertionError):
        check_round_trip([positive], expected, data, result)


# --- documented JSON limits ----------------------------------------------------------------------------------------


def test_adjacent_surrogates_do_not_round_trip() -> None:
    """A Python str holding a high then a low surrogate is escaped as two \\u escapes, which JSON reads as ONE astral
    character. Such text is not JSON-native (it is not valid Unicode) and is excluded from generation; lone ones survive."""
    pair = "\ud83d\ude00"
    assert json.loads(serialize_record({"a": pair})) == {"a": "\U0001f600"} != {"a": pair}
    assert json.loads(serialize_record({"a": "\ud83d"})) == {"a": "\ud83d"}


def test_values_that_json_cannot_carry_are_refused_not_altered() -> None:
    from coroner.evidence.writer import RecordError

    for bad in (float("nan"), float("inf"), {1: "x"}, b"bytes", {"a": {1, 2}}):
        with pytest.raises(RecordError):
            serialize_record({"record_type": "gap", "x": bad})
    assert json.loads(serialize_record({"x": (1, 2)})) == {"x": [1, 2]}  # a tuple comes back as a list: excluded from generation
