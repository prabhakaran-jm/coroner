"""Property 2: Reader quarantines bad lines and keeps going (requirements 5.10, 13.13).
*For any* evidence file made of valid record lines with arbitrary invalid lines (invalid UTF-8, invalid JSON,
non-object JSON, an incomplete last line, or a line isolated by a Repair_Record) interspersed, the Evidence_Reader
returns all valid records in their original order, reports exactly one error per invalid line carrying the correct
file name, line number, and byte length, never returns an invalid line among the records, computes `sample_count`
as the number of valid `sample` records, and does not modify the file.
**Validates: Requirements 5.10, 13.13**

Independent oracle. The generator builds the file from "units" (one line, or a fragment line plus the repair record
that names it) and computes the expected outcome of every line while it renders the bytes: expected records with
their line numbers, and expected errors with line number, byte length (no newline), kind and `has_repair_record`.
It uses only `json`, `base64` and `hashlib`; nothing from `coroner.evidence.reader` or `repair` is consulted, and the
expected kind strings are literals. Every generated invalid line is checked, in the generator, with `bytes.decode`
and `json.loads` to be invalid for the reason it is labelled with, and to contain no newline.

Kinds follow the design (D19): a fragment named by a repair record with action `appended_newline` is
`isolated_by_repair` whatever its content (even a complete, valid Sample); a line named by a repair record with action
`none_needed_already_terminated` keeps its own kind and has `has_repair_record` true; an unterminated last line with no
repair is `incomplete_last_line`; every other invalid line has `has_repair_record` false.
Files are `run-1.jsonl` or `_rejections.jsonl`, so the file-context rule (D27) is part of the oracle.
"""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from coroner.evidence.reader import ReadResult, read_evidence, summarize
from coroner.evidence.schema import validate_record

RUN_ID = "run-1"
RECV = "2025-01-01T12:00:06.530Z"
NAMES = {"run": RUN_ID + ".jsonl", "rejections": "_rejections.jsonl"}
INVALID_KINDS = ("invalid_utf8", "invalid_json", "not_an_object", "invalid_record")


# --- oracle ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    tag: str  # "valid", "bad", "isolated" (fragment + repair appended_newline) or "terminated" (line + repair none_needed)
    raw: bytes  # the line, without newline
    kind: str  # "valid" or the line's own error kind
    record: dict | None = None  # the expected record, for "valid"


@dataclass(frozen=True)
class Err:
    line_no: int
    byte_length: int
    kind: str
    has_repair: bool


@dataclass
class Expected:
    data: bytes
    records: list[dict]
    line_numbers: list[int]
    errors: list[Err]
    sample_count: int


def dumps(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("ascii")


def sample_record(seq: int, run_id: str | None = RUN_ID, rss: int = 1000) -> dict:
    sample = {
        "run_id": run_id, "seq": seq, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": rss, "request_count": 9,
        "loop_delay_ms": 0.84, "pid": 1, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }
    return {"record_type": "sample", "schema_version": 1, "run_id": run_id, "recv_time": RECV, "sample": sample}


def other_record(record_type: str, run_id: str | None, tag: int) -> dict:
    """A record with a schema-valid payload (task 2.19): a rejection, or a termination for every other type name."""
    env = {"record_type": record_type, "schema_version": 1, "run_id": run_id, "recv_time": RECV}
    if record_type == "rejection":
        return {**env, "http_status": 422, "reason_code": "invalid_json", "field": "$", "reason": "x\u2028y", "body_len": 3 + tag, "body_preview_b64": "eHh4"}
    return {**env, "source": "experiment_runner", "observed_time": RECV, "container_state": {"status": "exited", "exit_code": tag, "oom_killed": False},
            "cause_label": "unknown", "note": "x\u2028y"}


def repair_record(file_kind: str, line_no: int, raw: bytes, action: str) -> dict:
    reason = {"appended_newline": "incomplete_last_line", "none_needed_already_terminated": "invalid_json_last_line"}[action]
    return {
        "record_type": "repair", "schema_version": 1, "run_id": None if file_kind == "rejections" else RUN_ID, "recv_time": RECV,
        "file": NAMES[file_kind], "line_no": line_no, "fragment_len": len(raw), "fragment_b64": base64.b64encode(raw).decode("ascii"),
        "fragment_truncated": False, "reason": reason, "action": action,
    }


def render(file_kind: str, units: list[Unit], tail: bytes | None) -> Expected:
    lines: list[bytes] = []
    exp = Expected(b"", [], [], [], 0)

    def valid(record: dict) -> None:
        exp.records.append(record)
        exp.line_numbers.append(len(lines))  # called after the line was appended: its 1-based number
        if record["record_type"] == "sample":
            exp.sample_count += 1

    for unit in units:
        lines.append(unit.raw)
        number = len(lines)
        if unit.tag == "valid":
            assert unit.record is not None
            valid(unit.record)
        elif unit.tag == "bad":
            exp.errors.append(Err(number, len(unit.raw), unit.kind, False))
        else:
            action = "appended_newline" if unit.tag == "isolated" else "none_needed_already_terminated"
            exp.errors.append(Err(number, len(unit.raw), "isolated_by_repair" if unit.tag == "isolated" else unit.kind, True))
            repair = repair_record(file_kind, number, unit.raw, action)
            lines.append(dumps(repair))
            valid(repair)
    exp.data = b"".join(line + b"\n" for line in lines)
    if tail is not None:
        exp.data += tail
        exp.errors.append(Err(len(lines) + 1, len(tail), "incomplete_last_line", False))
    return exp


# --- generators -----------------------------------------------------------------------------------------------------

ASCII = st.text(st.characters(min_codepoint=32, max_codepoint=126), max_size=30)


def is_invalid_json(raw: bytes) -> bool:
    def refuse(name: str) -> None:
        raise ValueError(name)

    try:
        json.loads(raw.decode("utf-8"), parse_constant=refuse)
    except ValueError:
        return True
    return False


def is_invalid_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


FIXED_BAD_JSON = [b"", b"   ", b"\t", b"{", b'{"a":', b'{"a":1,}', b"not json", b"{'a':1}", b'{"a":NaN}', b'{"a":Infinity}', b"NaN", b"{}}",
                  b'{"a":1} x', b"[1,", b'"open']
BAD_JSON = st.one_of(
    st.sampled_from(FIXED_BAD_JSON),
    ASCII.map(lambda s: s.encode("ascii")),
    st.tuples(st.integers(0, 10**6), st.integers(1, 60)).map(lambda t: dumps(sample_record(t[0]))[: -t[1]]),  # a strict prefix of an object
).filter(is_invalid_json)

BAD_UTF8 = st.tuples(
    ASCII.map(lambda s: s.encode("ascii")),
    st.sampled_from([b"\xff", b"\xc3\x28", b"\xe2\x82", b"\x80", b"\xf8\x88\x80\x80\x80", b"\xed\xa0\x80"]),
    ASCII.map(lambda s: s.encode("ascii")),
).map(b"".join).filter(is_invalid_utf8)

JSON_LEAF = st.one_of(st.none(), st.booleans(), st.integers(-(2**70), 2**70), st.floats(allow_nan=False, allow_infinity=False), ASCII)
NON_OBJECT = st.one_of(JSON_LEAF, st.lists(JSON_LEAF, max_size=4), st.just(-0.0)).map(dumps)


def invalid_records(file_kind: str) -> list[dict]:
    """Complete JSON objects that are not valid records, for this file."""
    common = [{}, {"record_type": "sample"}, {"a": 1}, {**sample_record(1), "schema_version": 2}, {**other_record("gap", RUN_ID, 1), "record_type": "bogus"}]
    if file_kind == "run":
        return common + [sample_record(2, "other-run"), other_record("rejection", None, 2), other_record("termination", None, 3)]
    return common + [sample_record(2, RUN_ID), other_record("rejection", RUN_ID, 2), other_record("termination", None, 3)]


def valid_records(file_kind: str) -> list[dict]:
    if file_kind == "run":
        return [sample_record(i) for i in (0, 1, 7)] + [other_record("termination", RUN_ID, 1), other_record("rejection", RUN_ID, 2)]
    return [other_record("rejection", None, 1), other_record("rejection", None, 2)]


@st.composite
def units(draw, file_kind: str, allow_valid: bool = True) -> Unit:
    """One unit with its own expected outcome. `allow_valid` is False for the interspersed extras of the metamorphic check."""
    tag = draw(st.sampled_from(["valid", "valid", "bad", "bad", "isolated", "terminated"] if allow_valid else ["bad"]))
    if tag == "valid":
        if file_kind == "run" and draw(st.booleans()):
            record = sample_record(draw(st.integers(0, 10**9)), rss=draw(st.integers(0, 2**40)))
        else:
            record = draw(st.sampled_from(valid_records(file_kind)))
        assert validate_record(record) == []
        return Unit("valid", dumps(record), "valid", record)
    kind = draw(st.sampled_from(INVALID_KINDS))
    if kind == "invalid_utf8":
        raw = draw(BAD_UTF8)
    elif kind == "invalid_json":
        raw = draw(BAD_JSON)
    elif kind == "not_an_object":
        raw = draw(NON_OBJECT)
    else:
        raw = dumps(draw(st.sampled_from(invalid_records(file_kind))))
    if tag == "bad":
        return Unit("bad", raw, kind)
    if tag == "isolated" and draw(st.booleans()):
        # a fragment that is a complete, valid record (even a Sample) is still isolated by the repair record
        fragment = dumps(draw(st.sampled_from(valid_records(file_kind) + [sample_record(5), sample_record(6)] if file_kind == "run" else valid_records(file_kind))))
        return Unit("isolated", fragment, "valid")
    if not raw:  # a repair record names a non-empty fragment
        raw, kind = b"{", "invalid_json"
    return Unit(tag, raw, kind)


@st.composite
def tails(draw, file_kind: str) -> bytes | None:
    """An unterminated last line (no repair), or None."""
    if not draw(st.booleans()):
        return None
    choice = draw(st.sampled_from(["partial", "whole_record", "ascii", "utf8"]))
    if choice == "partial":
        return dumps(sample_record(draw(st.integers(0, 99))))[: draw(st.integers(1, 40))]
    if choice == "whole_record":
        return dumps(draw(st.sampled_from(valid_records(file_kind))))  # parses, but was never acknowledged
    if choice == "ascii":
        return draw(ASCII.filter(bool)).encode("ascii")
    return draw(BAD_UTF8)


@st.composite
def plans(draw) -> tuple[str, list[Unit], bytes | None, list[tuple[int, Unit]]]:
    file_kind = draw(st.sampled_from(["run", "run", "rejections"]))
    body = draw(st.lists(units(file_kind), max_size=12))
    tail = draw(tails(file_kind))
    extras = draw(st.lists(st.tuples(st.integers(0, 12), units(file_kind, allow_valid=False)), max_size=5))
    return file_kind, body, tail, extras


# --- checks ---------------------------------------------------------------------------------------------------------


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assert_matches(exp: Expected, result: ReadResult, file_name: str) -> None:
    assert result.records == exp.records, "valid records differ from the oracle (order, content or membership)"
    assert result.line_numbers == exp.line_numbers
    got = [(e.line_no, e.byte_length, e.kind, e.has_repair_record) for e in result.errors]
    assert got == [(e.line_no, e.byte_length, e.kind, e.has_repair) for e in exp.errors], "errors differ from the oracle"
    assert all(e.file == file_name for e in result.errors)
    assert len({e.line_no for e in result.errors}) == len(result.errors)  # exactly one error per line
    assert not {e.line_no for e in result.errors} & set(result.line_numbers)  # no line is both a record and an error
    summary = summarize(result)
    assert summary.sample_count == exp.sample_count
    assert summary.malformed_line_count == len(exp.errors)
    assert [e.line_no for e in summary.read_errors] == [e.line_no for e in exp.errors]


def check_plan(file_kind: str, body: list[Unit], tail: bytes | None) -> tuple[Expected, ReadResult]:
    """Write the oracle's bytes, read them through a Path and through bytes, and compare everything with the oracle."""
    name = NAMES[file_kind]
    exp = render(file_kind, body, tail)
    for unit in body:  # the generator's own guarantees: the invalid lines really are invalid, and nothing contains a newline
        assert b"\n" not in unit.raw
        if unit.kind == "invalid_utf8":
            assert is_invalid_utf8(unit.raw)
        elif unit.kind == "invalid_json":
            assert not is_invalid_utf8(unit.raw) and is_invalid_json(unit.raw)
        elif unit.kind == "not_an_object":
            assert not isinstance(json.loads(unit.raw), dict)
        elif unit.kind == "invalid_record":
            assert isinstance(json.loads(unit.raw), dict)
    assert tail is None or (b"\n" not in tail and tail != b"")
    with tempfile.TemporaryDirectory(prefix="coroner-p2-") as tmp:
        path = Path(tmp) / name
        path.write_bytes(exp.data)
        before = (sha(exp.data), len(exp.data))
        result = read_evidence(path)
        assert (sha(path.read_bytes()), path.stat().st_size) == before, "the reader modified the file"
        from_bytes = read_evidence(exp.data, name=name)
        again = read_evidence(path)
    assert_matches(exp, result, name)
    assert (from_bytes.records, from_bytes.line_numbers, from_bytes.errors) == (result.records, result.line_numbers, result.errors)  # Path == bytes
    assert again == result  # deterministic
    return exp, result


SETTINGS = settings(
    max_examples=150, deadline=None, derandomize=True, database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.large_base_example],
)


@SETTINGS
@given(plans())
def test_property_2_reader_quarantines_bad_lines_and_keeps_going(plan: tuple[str, list[Unit], bytes | None, list[tuple[int, Unit]]]) -> None:
    file_kind, body, tail, extras = plan
    exp, result = check_plan(file_kind, body, tail)
    # Metamorphic: interspersing more Malformed_Lines (never between a fragment and its repair record, never after the
    # unterminated tail) leaves every valid record and the Sample count unchanged and adds exactly one error each.
    more = list(body)
    for position, extra in sorted(extras, key=lambda item: -item[0]):
        more.insert(min(position, len(more)), extra)
    exp2, result2 = check_plan(file_kind, more, tail)
    assert exp2.sample_count == exp.sample_count
    # (repair records are left out: their `line_no` legitimately shifts when lines are inserted before them)
    assert [r for r in result2.records if r["record_type"] != "repair"] == [r for r in result.records if r["record_type"] != "repair"]
    assert sum(r["record_type"] == "repair" for r in result2.records) == sum(r["record_type"] == "repair" for r in result.records)
    assert summarize(result2).sample_count == summarize(result).sample_count
    assert summarize(result2).malformed_line_count == summarize(result).malformed_line_count + len(extras)


# --- explicit examples (the same oracle) -----------------------------------------------------------------------------

S0, S1, S2 = (Unit("valid", dumps(sample_record(i)), "valid", sample_record(i)) for i in range(3))


def bad(kind: str, raw: bytes) -> Unit:
    return Unit("bad", raw, kind)


def test_empty_file_has_no_records_and_no_errors() -> None:
    exp, result = check_plan("run", [], None)
    assert exp.data == b"" and result.records == [] and result.errors == [] and summarize(result).sample_count == 0


def test_only_invalid_lines_yield_no_records_and_one_error_each() -> None:
    body = [bad("invalid_utf8", b"\xff\xfe"), bad("invalid_json", b"{"), bad("not_an_object", b"[1]"), bad("not_an_object", b"null"), bad("invalid_json", b"")]
    exp, result = check_plan("run", body, b'{"record_type":')
    assert result.records == [] and len(result.errors) == 6
    assert [e.kind for e in result.errors] == ["invalid_utf8", "invalid_json", "not_an_object", "not_an_object", "invalid_json", "incomplete_last_line"]
    assert [e.byte_length for e in result.errors] == [2, 1, 3, 4, 0, 15]  # no newline counted
    assert summarize(result).sample_count == 0 and summarize(result).malformed_line_count == 6


def test_valid_before_and_after_malformed_lines_keep_order_and_line_numbers() -> None:
    body = [S0, bad("invalid_json", b"oops"), S1, bad("not_an_object", b'"s"'), bad("invalid_utf8", b"a\xffb"), S2]
    exp, result = check_plan("run", body, None)
    assert [r["sample"]["seq"] for r in result.records] == [0, 1, 2] and result.line_numbers == [1, 3, 6]
    assert [e.line_no for e in result.errors] == [2, 4, 5] and summarize(result).sample_count == 3


def test_a_valid_json_sample_isolated_by_a_repair_record_is_quarantined_and_not_counted() -> None:
    body = [S0, Unit("isolated", dumps(sample_record(1)), "valid"), S2]
    exp, result = check_plan("run", body, None)
    assert [(e.line_no, e.kind, e.has_repair_record) for e in result.errors] == [(2, "isolated_by_repair", True)]
    assert result.line_numbers == [1, 3, 4]  # S0, the repair record, S2
    assert summarize(result).sample_count == 2  # the isolated Sample and the repair record are not counted


def test_a_terminated_invalid_line_with_a_repair_record_keeps_its_own_kind() -> None:
    exp, result = check_plan("run", [S0, Unit("terminated", b"{bad", "invalid_json")], None)
    assert [(e.kind, e.has_repair_record) for e in result.errors] == [("invalid_json", True)]


def test_the_rejections_file_context_is_part_of_the_oracle() -> None:
    rej = other_record("rejection", None, 1)
    body = [Unit("valid", dumps(rej), "valid", rej), Unit("bad", dumps(sample_record(3)), "invalid_record"), Unit("isolated", b'{"x":', "invalid_json")]
    exp, result = check_plan("rejections", body, None)
    assert [e.kind for e in result.errors] == ["invalid_record", "isolated_by_repair"] and summarize(result).sample_count == 0


# --- negative control: the oracle comparison detects a reader that misbehaves ---------------------------------------


@pytest.mark.parametrize("fault", ["line_off_by_one", "length_with_newline", "quarantined_kept", "repair_counted", "kind_lost"])
def test_the_oracle_comparison_detects_a_faulty_reader_result(fault: str) -> None:
    exp = render("run", [S0, bad("invalid_json", b"oops"), Unit("isolated", dumps(sample_record(1)), "valid"), S2], None)
    good = read_evidence(exp.data, name=NAMES["run"])
    assert_matches(exp, good, NAMES["run"])
    from dataclasses import replace

    if fault == "line_off_by_one":
        good.errors[0] = replace(good.errors[0], line_no=good.errors[0].line_no + 1)
    elif fault == "length_with_newline":
        good.errors[0] = replace(good.errors[0], byte_length=good.errors[0].byte_length + 1)
    elif fault == "quarantined_kept":
        good.records.insert(1, sample_record(1))
        good.line_numbers.insert(1, 3)
    elif fault == "repair_counted":
        good.records.append(sample_record(9))
        good.line_numbers.append(99)
    else:
        good.errors[1] = replace(good.errors[1], kind="invalid_json")
    with pytest.raises(AssertionError):
        assert_matches(exp, good, NAMES["run"])
