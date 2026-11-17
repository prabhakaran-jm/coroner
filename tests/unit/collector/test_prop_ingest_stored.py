"""Property 3: Valid Samples are stored exactly as received (Task 3.6).

**Validates: Requirements 4.2, 4.7, 5.2, 7.5, 12.1**

Contract tested through the real `IngestService` (real files in a temporary directory, injected clock and fsync
doubles): ingesting a schema-valid Sample appends one `sample` record whose nested `sample` is the received object
(same JSON types, same signs of zero, same key order), whose envelope has exactly the keys `record_type`,
`schema_version`, `run_id`, `recv_time`, `sample`, whose `run_id` is the Sample's and whose `recv_time` is the
injected receive time in UTC millisecond format. Nothing is added to or changed in the Sample, and a later ingest does
not alter bytes already stored.

Independent oracle: the expected file lines are built here from the generated input and the injected clock
(`json.dumps`, compact, ASCII, one `\\n`), never from what the Collector stored. A Sample whose `seq` is not 0 or whose
`dropped` is not 0 also makes gap records after the Sample record; those are checked only for type, run and
`detected_at_seq`, because gap arithmetic is Property 7 to 9 territory.
"""
from __future__ import annotations

import json
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from coroner.collector.config import CollectorConfig
from coroner.collector.service import IngestService, create_ingest_service
from coroner.common.timeutil import format_utc_ms, parse_utc_ms
from coroner.evidence.reader import read_evidence
from coroner.evidence.schema import validate_sample
from coroner.evidence.writer import Durability

ENVELOPE_KEYS = ["record_type", "schema_version", "run_id", "recv_time", "sample"]
NULLABLE = ("rss_bytes", "request_count", "loop_delay_ms", "container_id")
SOURCES = ("mountinfo", "cgroup", "hostname", "none")
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


# --- independent oracle ------------------------------------------------------------------------------------


def strict_equal(a: object, b: object) -> bool:
    """Equality that also requires the same Python type (1, 1.0 and True differ), key order, and the sign of zero."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(strict_equal(a[k], b[k]) for k in a)  # type: ignore[arg-type]
    if isinstance(a, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))  # type: ignore[arg-type]
    if isinstance(a, float):
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)  # type: ignore[arg-type]
    return a == b


def line_of(record: dict[str, Any]) -> bytes:
    """The one line a record must occupy: compact ASCII JSON and a single newline."""
    return json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"


def expected_metadata(run_id: str, recv: str, durability: Durability) -> dict[str, Any]:
    return {
        "record_type": "run_metadata", "schema_version": 1, "run_id": run_id, "recv_time": recv, "event": "created",
        "durability": durability.value, "fsync_before_ack": durability is Durability.CRASH,
        "evidence_file": f"{run_id}.jsonl", "collector_version": "0.1.0",
    }


def expected_sample_record(sample: dict[str, Any], recv: str) -> dict[str, Any]:
    return {"record_type": "sample", "schema_version": 1, "run_id": sample["run_id"], "recv_time": recv, "sample": sample}


def split_lines(data: bytes) -> list[bytes]:
    assert data.endswith(b"\n"), "file does not end with a newline"
    lines = [part + b"\n" for part in data.split(b"\n")[:-1]]
    assert all(len(line) > 1 and b"\r" not in line for line in lines), "empty line or raw carriage return"
    return lines


def check_first_ingest(sample: dict[str, Any], recv: str, durability: Durability, data: bytes) -> list[bytes]:
    """Raise AssertionError unless `data` is the file after the first ingest of `sample` received at `recv`."""
    lines = split_lines(data)
    assert len(lines) >= 2
    assert lines[0] == line_of(expected_metadata(sample["run_id"], recv, durability)), "run_metadata line differs"
    want = expected_sample_record(sample, recv)
    assert lines[1] == line_of(want), "sample line differs byte for byte"
    for extra in lines[2:]:
        gap = json.loads(extra)
        assert gap["record_type"] == "gap" and gap["run_id"] == sample["run_id"] and gap["detected_at_seq"] == sample["seq"]
    if sample["seq"] == 0 and sample["dropped"] == 0:
        assert len(lines) == 2, "a first Sample 0 without drops must write no gap"
    return lines


def check_read_back(sample: dict[str, Any], recv: str, path: Path) -> None:
    """The reader's view: no errors, and the stored sample record is strictly equal to the oracle's."""
    result = read_evidence(path)
    assert result.errors == []
    sample_records = [r for r in result.records if r["record_type"] == "sample"]
    assert len(sample_records) >= 1
    got = sample_records[0]
    assert list(got) == ENVELOPE_KEYS, f"envelope keys are {list(got)}"
    assert got["run_id"] == sample["run_id"] and got["recv_time"] == recv
    parse_utc_ms(got["recv_time"])
    assert strict_equal(got["sample"], sample), "nested sample differs from the received object"
    assert set(got["sample"]) == set(sample), "a Sample field was added or dropped"


# --- strategies (built from the schema rules; the Collector is never called to build them) -------------------

_HEX = "0123456789abcdef"
_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

run_ids = (
    st.tuples(st.sampled_from(_ALNUM), st.text(alphabet=_ALNUM + "._-", min_size=0, max_size=63))
    .map(lambda t: t[0] + t[1])
    .filter(lambda s: s.split(".")[0].lower() not in _WINDOWS_DEVICES)  # reserved device names cannot be files on Windows
)
seqs = st.one_of(st.sampled_from([0, 1, 2, 2**31 - 1, 2**32, 2**53, 2**63 - 1, 2**64]), st.integers(0, 2**64))
counters = st.one_of(st.sampled_from([0, 1, 2**31, 2**53, 2**63 - 1, 2**63, 2**64 - 1]), st.integers(0, 2**64))
loop_delays = st.one_of(
    st.sampled_from([0, 0.0, -0.0, 5, 5.0, 0.1, 0.123456789012345, 1e-7, 1e22, 5e-324, 1.7976931348623157e308, 1e16 + 2]),
    st.integers(0, 10**9),
    st.floats(min_value=0.0, allow_nan=False, allow_infinity=False),
)
container_ids = st.one_of(
    st.text(alphabet=_HEX, min_size=64, max_size=64), st.text(alphabet=_HEX, min_size=12, max_size=12),
)
_TRICKY = ['"', "\\", "\n", "\r", "\t", "\x00", "\x7f", "é", "\u0301", "\u202e", "\u05d0", "\U0001f600", "\u2028", "\u2029", "/", "'", "{", "[", "\ufeff"]
reasons = st.one_of(
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=200),
    st.text(alphabet=st.sampled_from(_TRICKY), min_size=1, max_size=200),
    st.sampled_from(["x", "x" * 200, "\U0001f600" * 200, "\\" * 200, '"' * 200, "e\u0301" * 100, "\u202e" * 200]),
)
timestamps = st.datetimes(
    min_value=datetime(1970, 1, 1), max_value=datetime(2200, 1, 1), timezones=st.just(timezone.utc)
).map(format_utc_ms)
styles = st.sampled_from([(None, (",", ":")), (None, (", ", ": ")), (1, None), (2, None)])
pads = st.sampled_from(["", " ", "\n", "\t \r\n"])


@st.composite
def cases(draw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "run_id": draw(run_ids), "seq": draw(seqs), "ts": draw(timestamps),
        "rss_bytes": draw(st.one_of(st.none(), counters)), "request_count": draw(st.one_of(st.none(), counters)),
        "loop_delay_ms": draw(st.one_of(st.none(), loop_delays)), "pid": draw(st.one_of(st.integers(-(2**31), 2**63), st.sampled_from([0, 1, -1]))),
        "container_id": draw(st.one_of(st.none(), container_ids)), "container_id_source": draw(st.sampled_from(SOURCES)),
        "interval_ms": draw(st.one_of(st.sampled_from([100, 60000, 101, 59999, 500]), st.integers(100, 60000))),
        "dropped": draw(st.one_of(st.just(0), st.sampled_from([1, 2**32 - 1]), st.integers(0, 2**32 - 1))),
    }
    null_reasons = {f: draw(reasons) for f in NULLABLE if values[f] is None}
    with_reasons = bool(null_reasons) or draw(st.booleans())  # an empty object is valid when nothing is null
    keys = draw(st.permutations([*values] + (["null_reasons"] if with_reasons else [])))
    obj: dict[str, Any] = {}
    for key in keys:
        if key == "null_reasons":
            obj[key] = {f: null_reasons[f] for f in draw(st.permutations(list(null_reasons)))}
        else:
            obj[key] = values[key]
    ascii_only = draw(st.booleans())
    return {
        "sample": obj, "ensure_ascii": ascii_only, "style": draw(styles), "pad": draw(pads),
        "recv1": draw(timestamps), "recv2": draw(timestamps), "durability": draw(st.sampled_from([Durability.CRASH, Durability.PROCESS])),
    }


def encode(obj: dict[str, Any], *, ensure_ascii: bool, style: tuple[Any, Any], pad: str) -> bytes:
    indent, separators = style
    text = json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, separators=separators)
    return (pad + text + pad).encode("utf-8")


# --- harness -----------------------------------------------------------------------------------------------


class Clock:
    def __init__(self, value: str) -> None:
        self.value = value

    def now(self) -> str:
        return self.value


def build_service(root: Path, durability: Durability, clock: Clock) -> IngestService:
    config = CollectorConfig(durability=durability, max_body_bytes=65536, watchdog_tick_ms=50, evidence_dir=root / "evidence")
    service = create_ingest_service(config, fsync=lambda fd: None, fsync_dir=lambda path: None, now=clock.now)
    service.registry.start()
    return service


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """Ingest the case's Sample, then the next one; check both against the oracle. Returns facts for the callers."""
    sample = case["sample"]
    assert validate_sample(sample) == [], "the strategy produced an invalid Sample"
    raw = encode(sample, ensure_ascii=case["ensure_ascii"], style=case["style"], pad=case["pad"])
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock(case["recv1"])
        service = build_service(Path(tmp), case["durability"], clock)
        try:
            result = service.ingest(raw)
            assert result.http_status == 200 and result.kind == "stored", result.body
            assert result.body == {
                "status": "stored", "run_id": sample["run_id"], "seq": sample["seq"],
                "durability": case["durability"].value, "recv_time": case["recv1"],
            }
            path = service.registry.writer.path_for(sample["run_id"])
            first_bytes = path.read_bytes()
            check_first_ingest(sample, case["recv1"], case["durability"], first_bytes)
            check_read_back(sample, case["recv1"], path)

            second = dict(sample)  # same key order, next sequence number, no drop
            second["seq"], second["dropped"] = sample["seq"] + 1, 0
            clock.value = case["recv2"]
            result2 = service.ingest(encode(second, ensure_ascii=case["ensure_ascii"], style=case["style"], pad=case["pad"]))
            assert result2.http_status == 200 and result2.kind == "stored"
            second_bytes = path.read_bytes()
            assert second_bytes.startswith(first_bytes), "a later ingest changed bytes already stored"
            assert second_bytes[len(first_bytes):] == line_of(expected_sample_record(second, case["recv2"])), "the second line differs"
            assert read_evidence(path).errors == []
            others = [p.name for p in (Path(tmp) / "evidence").glob("*.jsonl")]
            assert others == [f"{sample['run_id']}.jsonl"]
        finally:
            service.registry.close()
    return {"result": result}


SETTINGS = settings(
    max_examples=150, deadline=None, derandomize=True, database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.large_base_example, HealthCheck.filter_too_much],
)


@SETTINGS
@given(cases())
def test_property_3_valid_samples_are_stored_exactly_as_received(case: dict[str, Any]) -> None:
    run_case(case)


# --- explicit hard cases (same oracle) -----------------------------------------------------------------------


def base_sample(**over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "run_id": "r-1", "seq": 0, "ts": "2025-01-01T12:00:06.512Z", "rss_bytes": 1000, "request_count": 1,
        "loop_delay_ms": 0.5, "pid": 7, "container_id": "3f1c0e0a9d7b", "container_id_source": "mountinfo",
        "interval_ms": 500, "dropped": 0,
    }
    out.update(over)
    return out


def case_for(sample: dict[str, Any], **over: Any) -> dict[str, Any]:
    case = {
        "sample": sample, "ensure_ascii": True, "style": (None, (",", ":")), "pad": "",
        "recv1": "2030-06-07T08:09:10.011Z", "recv2": "2030-06-07T08:09:11.022Z", "durability": Durability.CRASH,
    }
    case.update(over)
    return case


@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_negative_zero_keeps_its_sign(ensure_ascii: bool) -> None:
    sample = base_sample(loop_delay_ms=-0.0)
    run_case(case_for(sample, ensure_ascii=ensure_ascii))
    with tempfile.TemporaryDirectory() as tmp:
        service = build_service(Path(tmp), Durability.CRASH, Clock("2030-01-01T00:00:00.000Z"))
        service.ingest(encode(sample, ensure_ascii=ensure_ascii, style=(None, (",", ":")), pad=""))
        stored = read_evidence(service.registry.writer.path_for("r-1")).records[1]["sample"]["loop_delay_ms"]
        service.registry.close()
    assert math.copysign(1.0, stored) == -1.0 and isinstance(stored, float)


def test_integer_and_float_forms_of_the_same_number_are_not_converted() -> None:
    run_case(case_for(base_sample(loop_delay_ms=5, rss_bytes=2**63, request_count=2**64 - 1)))
    run_case(case_for(base_sample(loop_delay_ms=5.0, rss_bytes=0)))


def test_nulls_with_reasons_and_an_empty_reasons_object_are_unchanged() -> None:
    nulls = base_sample(rss_bytes=None, request_count=None, loop_delay_ms=None, container_id=None, container_id_source="none")
    nulls["null_reasons"] = {"container_id": 'a "quoted" \\ reason\nwith a newline', "rss_bytes": "\U0001f600", "request_count": "e\u0301", "loop_delay_ms": "\u202e" * 200}
    run_case(case_for(nulls, ensure_ascii=False))
    run_case(case_for(base_sample(null_reasons={})))


def test_ts_and_recv_time_stay_separate_and_boundaries_are_kept() -> None:
    sample = base_sample(seq=2**64, ts="1970-01-01T00:00:00.000Z", interval_ms=60000, dropped=2**32 - 1, container_id="a" * 64)
    run_case(case_for(sample, recv1="2200-01-01T00:00:00.000Z"))
    run_case(case_for(base_sample(interval_ms=100, ts="2030-06-07T08:09:10.011Z")))  # ts equal to recv_time is also fine


def test_the_record_is_one_line_with_no_raw_newline_even_for_u2028_and_newlines_in_text() -> None:
    sample = base_sample(rss_bytes=None, null_reasons={"rss_bytes": "a\nb\u2028c\u2029d\r\ne"})
    run_case(case_for(sample, ensure_ascii=False))


# --- negative controls: the oracle must notice a wrongly stored Sample ----------------------------------------


def stored_bytes(sample: dict[str, Any], recv: str = "2030-06-07T08:09:10.011Z") -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        service = build_service(Path(tmp), Durability.CRASH, Clock(recv))
        service.ingest(encode(sample, ensure_ascii=True, style=(None, (",", ":")), pad=""))
        data = service.registry.writer.path_for(sample["run_id"]).read_bytes()
        service.registry.close()
    return data


def test_oracle_accepts_the_real_result_and_rejects_altered_ones() -> None:
    sample = base_sample(loop_delay_ms=0.123456, rss_bytes=1234567)
    recv = "2030-06-07T08:09:10.011Z"
    data = stored_bytes(sample, recv)
    check_first_ingest(sample, recv, Durability.CRASH, data)  # the real bytes pass
    altered = {
        "rounded float": data.replace(b"0.123456", b"0.12"),
        "float for int": data.replace(b'"rss_bytes":1234567', b'"rss_bytes":1234567.0'),
        "extra envelope key": data.replace(b'"sample":{', b'"extra":1,"sample":{', 1),
        "changed recv_time": data.replace(b'"recv_time":"2030-06-07T08:09:10.011Z","sample"', b'"recv_time":"2030-06-07T08:09:10.012Z","sample"'),
        "no trailing newline": data[:-1],
        "extra field added": data.replace(b'"dropped":0}', b'"dropped":0,"x":1}'),
    }
    for name, bad in altered.items():
        assert bad != data, name
        with pytest.raises(AssertionError):
            check_first_ingest(sample, recv, Durability.CRASH, bad)


def test_strict_equal_distinguishes_types_and_the_sign_of_zero() -> None:
    assert not strict_equal({"a": 1}, {"a": 1.0})
    assert not strict_equal({"a": 1}, {"a": True})
    assert not strict_equal({"a": 0.0}, {"a": -0.0})
    assert not strict_equal({"a": 1, "b": 2}, {"b": 2, "a": 1})
    assert strict_equal({"a": [1, 0.5, None]}, {"a": [1, 0.5, None]})
