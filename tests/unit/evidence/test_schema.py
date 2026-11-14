"""Tests for coroner.evidence.schema (tasks 2.2 and 2.3; requirements 4.5, 4.9, 7.7).

These call the real module. The example records are copied from design.md, "Data Models".
The last section was added by task 2.3 after a coverage audit of the task text.
"""
from __future__ import annotations

import copy
import json
import math

import pytest

from coroner.evidence import schema
from coroner.evidence.schema import (
    RECORD_TYPES,
    RUN_ID_RE,
    SCHEMA_VERSION,
    ValidationIssue,
    validate_record,
    validate_run_id,
    validate_sample,
)

VALID_SAMPLE_JSON = (
    '{"run_id":"a-20250101T120000Z-9f3a1c2e","seq":12,"ts":"2025-01-01T12:00:06.512Z","rss_bytes":71303168,'
    '"request_count":9,"loop_delay_ms":0.84,"pid":1,'
    '"container_id":"3f1c0e0a9d7b4c1e8a6f5d2b9c4e7a1033b8d5f6e2a4c7b9d1e3f5a7c9b1d3e5",'
    '"container_id_source":"mountinfo","interval_ms":500,"dropped":0}'
)
NULL_SAMPLE_JSON = (
    '{"run_id":"manual-1a2b3c4d","seq":3,"ts":"2025-01-01T12:00:01.501Z","rss_bytes":null,"request_count":2,'
    '"loop_delay_ms":0.31,"pid":1,"container_id":null,"container_id_source":"none","interval_ms":500,"dropped":0,'
    '"null_reasons":{"rss_bytes":"cannot read /proc/self/statm: FileNotFoundError",'
    '"container_id":"no container id found in mountinfo, cgroup, or HOSTNAME"}}'
)
STORED_SAMPLE_JSON = (
    '{"record_type":"sample","schema_version":1,"run_id":"a-20250101T120000Z-9f3a1c2e",'
    '"recv_time":"2025-01-01T12:00:06.530Z","sample":' + VALID_SAMPLE_JSON + "}"
)
OTHER_RECORD_JSON = {
    "run_metadata": '{"record_type":"run_metadata","schema_version":1,"run_id":"a-20250101T120000Z-9f3a1c2e","recv_time":"2025-01-01T12:00:06.021Z","event":"created","durability":"crash","fsync_before_ack":true,"evidence_file":"a-20250101T120000Z-9f3a1c2e.jsonl","collector_version":"0.1.0"}',
    "gap_sequence": '{"record_type":"gap","schema_version":1,"run_id":"a-20250101T120000Z-9f3a1c2e","recv_time":"2025-01-01T12:00:08.031Z","gap_id":"gap-0001","gap_type":"sequence_discontinuity","start_time":"2025-01-01T12:00:06.530Z","end_time":"2025-01-01T12:00:08.031Z","first_missing_seq":13,"last_missing_seq":15,"missing_count":3,"reconciliation":"reconciled","reconciles_with":["gap-0002"],"detected_at_seq":16}',
    "gap_drop_unknown": '{"record_type":"gap","schema_version":1,"run_id":"r1","recv_time":"2025-01-01T12:00:09.031Z","gap_id":"gap-0003","gap_type":"probe_reported_drop","start_time":"2025-01-01T12:00:08.531Z","end_time":"2025-01-01T12:00:09.031Z","dropped_count":1,"range_status":"unknown","first_missing_seq":null,"last_missing_seq":null,"range_note":"range overlaps received samples","reconciliation":"none","reconciles_with":[],"detected_at_seq":20}',
    "gap_silence": '{"record_type":"gap","schema_version":1,"run_id":"a-20250101T120000Z-9f3a1c2e","recv_time":"2025-01-01T12:00:12.081Z","gap_id":"gap-0004","gap_type":"silence_timeout","start_time":"2025-01-01T12:00:10.530Z","end_time":"2025-01-01T12:00:12.081Z","last_sample_time":"2025-01-01T12:00:10.530Z","detected_time":"2025-01-01T12:00:12.081Z","threshold_ms":1500,"interval_ms":500,"sample_count":"unknown","reconciliation":"not_applicable","reconciles_with":[]}',
    "sequence_anomaly": '{"record_type":"sequence_anomaly","schema_version":1,"run_id":"r1","recv_time":"2025-01-01T12:00:10.001Z","anomaly":"duplicate","seq":7,"previous_seq":9,"sample":{"run_id":"r1","seq":7,"ts":"2025-01-01T12:00:03.500Z","rss_bytes":50000000,"request_count":1,"loop_delay_ms":0.2,"pid":1,"container_id":null,"container_id_source":"none","interval_ms":500,"dropped":0,"null_reasons":{"container_id":"not found"}}}',
    "rejection_no_run": '{"record_type":"rejection","schema_version":1,"run_id":null,"recv_time":"2025-01-01T12:00:11.250Z","http_status":422,"reason_code":"invalid_run_id","field":"run_id","reason":"run_id longer than 64 characters","body_len":214,"body_preview_b64":"eyJydW5faWQiOi..."}',
    "repair": '{"record_type":"repair","schema_version":1,"run_id":"a-20250101T120000Z-9f3a1c2e","recv_time":"2025-01-01T12:05:00.004Z","file":"a-20250101T120000Z-9f3a1c2e.jsonl","line_no":143,"fragment_len":57,"fragment_b64":"eyJyZWNvcmRfdHlwZSI6InNhbXBsZSIsInNjaGVtYV92ZXJzaW9uIjox","fragment_truncated":false,"reason":"incomplete_last_line","action":"appended_newline"}',
    "termination": '{"record_type":"termination","schema_version":1,"run_id":"c-20250101T121500Z-4be07a11","recv_time":"2025-01-01T12:17:03.300Z","source":"experiment_runner","observed_time":"2025-01-01T12:17:03.221Z","container_state":{"status":"exited","exit_code":137,"oom_killed":true},"cause_label":"docker_oom_kill","note":"Observed after the fact via docker inspect."}',
}

REQUIRED = list(schema.SAMPLE_REQUIRED_FIELDS)
NULLABLE = list(schema.NULLABLE_SAMPLE_FIELDS)
HEX64 = "3f1c0e0a9d7b4c1e8a6f5d2b9c4e7a1033b8d5f6e2a4c7b9d1e3f5a7c9b1d3e5"


def good() -> dict:
    return json.loads(VALID_SAMPLE_JSON)


def pairs(issues: list[ValidationIssue]) -> list[tuple[str, str]]:
    return [(i.field, i.reason_code) for i in issues]


def only(issues: list[ValidationIssue]) -> tuple[str, str]:
    assert len(issues) == 1, issues
    return issues[0].field, issues[0].reason_code


def mutated(**changes: object) -> dict:
    sample = good()
    sample.update(changes)
    return sample


def without(*names: str) -> dict:
    sample = good()
    for name in names:
        del sample[name]
    return sample


# --- constants ---------------------------------------------------------------------


def test_constants_match_the_design() -> None:
    assert SCHEMA_VERSION == 1
    assert RUN_ID_RE.pattern == "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    assert RECORD_TYPES == ("run_metadata", "sample", "gap", "sequence_anomaly", "rejection", "repair", "termination")
    assert set(schema.REASON_CODES) == {
        "invalid_run_id", "missing_field", "wrong_type", "out_of_range", "invalid_format", "null_without_reason", "unknown_field",
    }
    assert schema.NULLABLE_SAMPLE_FIELDS == ("rss_bytes", "request_count", "loop_delay_ms", "container_id")
    assert schema.CONTAINER_ID_SOURCES == ("mountinfo", "cgroup", "hostname", "none")
    assert (schema.INTERVAL_MS_MIN, schema.INTERVAL_MS_MAX, schema.DROPPED_MAX) == (100, 60000, 4294967295)


def test_validation_issue_is_a_frozen_value() -> None:
    issue = ValidationIssue("seq", "out_of_range", "seq -1 is not at least 0")
    assert (issue.field, issue.reason_code, issue.reason) == ("seq", "out_of_range", "seq -1 is not at least 0")
    with pytest.raises(AttributeError):
        issue.field = "x"  # type: ignore[misc]
    assert issue == ValidationIssue("seq", "out_of_range", "seq -1 is not at least 0")


# --- valid Samples -----------------------------------------------------------------


@pytest.mark.parametrize("text", [VALID_SAMPLE_JSON, NULL_SAMPLE_JSON])
def test_design_example_samples_are_valid(text: str) -> None:
    assert validate_sample(json.loads(text)) == []


def test_valid_sample_survives_a_compact_json_round_trip() -> None:
    for text in (VALID_SAMPLE_JSON, NULL_SAMPLE_JSON):
        original = json.loads(text)
        again = json.loads(json.dumps(original, separators=(",", ":"), ensure_ascii=True, allow_nan=False))
        assert again == original
        assert validate_sample(again) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"seq": 0}, {"seq": 2**80}, {"rss_bytes": 0}, {"rss_bytes": 2**70}, {"request_count": 0},
        {"loop_delay_ms": 0}, {"loop_delay_ms": 0.0}, {"loop_delay_ms": -0.0}, {"loop_delay_ms": 3}, {"loop_delay_ms": 1e300},
        {"pid": 0}, {"pid": 1}, {"pid": 4194304},
        {"container_id": "0123456789ab"}, {"container_id": HEX64}, {"container_id_source": "mountinfo"},
        {"container_id_source": "cgroup"}, {"container_id_source": "hostname"}, {"container_id_source": "none"},
        {"interval_ms": 100}, {"interval_ms": 500}, {"interval_ms": 60000},
        {"dropped": 0}, {"dropped": 4294967295},
        {"ts": "0001-01-01T00:00:00.000Z"}, {"ts": "9999-12-31T23:59:59.999Z"}, {"ts": "2024-02-29T00:00:00.000Z"},
    ],
)
def test_valid_boundary_values(changes: dict) -> None:
    assert validate_sample(mutated(**changes)) == []


# --- required fields ---------------------------------------------------------------


@pytest.mark.parametrize("field", REQUIRED)
def test_each_required_field_is_required(field: str) -> None:
    expected = "invalid_run_id" if field == "run_id" else "missing_field"
    assert only(validate_sample(without(field))) == (field, expected)


def test_missing_nullable_field_is_rejected_even_though_null_is_allowed() -> None:
    for field in NULLABLE:
        assert only(validate_sample(without(field))) == (field, "missing_field")


def test_empty_object_reports_every_required_field() -> None:
    found = pairs(validate_sample({}))
    assert len(found) == len(REQUIRED)
    assert ("run_id", "invalid_run_id") in found
    assert {f for f, _ in found} == set(REQUIRED)


# --- unknown and extra fields ------------------------------------------------------


def test_unknown_field_is_rejected() -> None:
    assert only(validate_sample(mutated(extra=1))) == ("extra", "unknown_field")
    assert only(validate_sample(mutated(rss=1))) == ("rss", "unknown_field")
    assert only(validate_sample(mutated(RSS_BYTES=1))) == ("RSS_BYTES", "unknown_field")


def test_every_unknown_field_is_reported() -> None:
    found = pairs(validate_sample(mutated(a=1, b=2)))
    assert ("a", "unknown_field") in found and ("b", "unknown_field") in found


def test_receive_time_style_fields_are_not_part_of_a_sample() -> None:
    assert only(validate_sample(mutated(recv_time="2025-01-01T12:00:06.530Z"))) == ("recv_time", "unknown_field")


# --- wrong types -------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("seq", "12"), ("seq", 12.0), ("seq", True), ("seq", False), ("seq", None), ("seq", [12]),
        ("ts", 1735732800), ("ts", None), ("ts", True), ("ts", ["2025-01-01T12:00:06.512Z"]),
        ("rss_bytes", "71303168"), ("rss_bytes", 1.5), ("rss_bytes", 71303168.0), ("rss_bytes", True), ("rss_bytes", []),
        ("request_count", "9"), ("request_count", 9.0), ("request_count", False),
        ("loop_delay_ms", "0.84"), ("loop_delay_ms", True), ("loop_delay_ms", False), ("loop_delay_ms", [0.84]),
        ("pid", "1"), ("pid", 1.0), ("pid", True), ("pid", None),
        ("container_id", 12345), ("container_id", True), ("container_id", [HEX64]),
        ("container_id_source", 1), ("container_id_source", None), ("container_id_source", True),
        ("interval_ms", "500"), ("interval_ms", 500.0), ("interval_ms", True), ("interval_ms", None),
        ("dropped", "0"), ("dropped", 0.0), ("dropped", False), ("dropped", None),
    ],
)
def test_wrong_type_is_rejected(field: str, value: object) -> None:
    assert only(validate_sample(mutated(**{field: value}))) == (field, "wrong_type")


@pytest.mark.parametrize("value", [5, None, [], {}, True, 1.5])
def test_run_id_of_the_wrong_type_is_an_invalid_run_id(value: object) -> None:
    assert only(validate_sample(mutated(run_id=value))) == ("run_id", "invalid_run_id")


@pytest.mark.parametrize("value", [None, [], "sample", 5, True, 1.5])
def test_a_sample_that_is_not_an_object(value: object) -> None:
    assert pairs(validate_sample(value)) == [("$", "wrong_type")]


# --- booleans are not integers -----------------------------------------------------


@pytest.mark.parametrize("field", ["seq", "rss_bytes", "request_count", "pid", "interval_ms", "dropped"])
@pytest.mark.parametrize("value", [True, False])
def test_bool_is_never_an_integer(field: str, value: bool) -> None:
    assert only(validate_sample(mutated(**{field: value}))) == (field, "wrong_type")


# --- numeric boundaries ------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("seq", -1), ("seq", -(2**70)),
        ("rss_bytes", -1), ("request_count", -1),
        ("interval_ms", 99), ("interval_ms", 0), ("interval_ms", -500), ("interval_ms", 60001), ("interval_ms", 2**40),
        ("dropped", -1), ("dropped", 4294967296), ("dropped", 2**64),
        ("loop_delay_ms", -0.001), ("loop_delay_ms", -1), ("loop_delay_ms", -1e300),
        ("loop_delay_ms", float("nan")), ("loop_delay_ms", float("inf")), ("loop_delay_ms", float("-inf")),
    ],
)
def test_out_of_range_numbers_are_rejected(field: str, value: float) -> None:
    assert only(validate_sample(mutated(**{field: value}))) == (field, "out_of_range")


def test_nan_from_json_text_is_rejected() -> None:
    parsed = json.loads(VALID_SAMPLE_JSON.replace('"loop_delay_ms":0.84', '"loop_delay_ms":NaN'))
    assert math.isnan(parsed["loop_delay_ms"])
    assert only(validate_sample(parsed)) == ("loop_delay_ms", "out_of_range")
    parsed = json.loads(VALID_SAMPLE_JSON.replace('"loop_delay_ms":0.84', '"loop_delay_ms":Infinity'))
    assert only(validate_sample(parsed)) == ("loop_delay_ms", "out_of_range")


def test_json_float_that_looks_integral_is_still_a_float() -> None:
    parsed = json.loads(VALID_SAMPLE_JSON.replace('"seq":12', '"seq":12.0'))
    assert only(validate_sample(parsed)) == ("seq", "wrong_type")


# --- timestamps --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "", "2025-01-01T12:00:06Z", "2025-01-01T12:00:06.5Z", "2025-01-01T12:00:06.512345Z", "2025-01-01T12:00:06.512",
        "2025-01-01T12:00:06.512+00:00", "2025-01-01T12:00:06.512+02:00", "2025-01-01 12:00:06.512Z", "2025-01-01T12:00:06.512z",
        " 2025-01-01T12:00:06.512Z", "2025-01-01T12:00:06.512Z\n", "2025-13-01T12:00:06.512Z", "2025-02-30T12:00:06.512Z",
        "2025-01-01T25:00:06.512Z", "2025-01-01T12:00:60.512Z", "0000-01-01T00:00:00.000Z", "yesterday", "1735732800",
        "２０２５-01-01T12:00:06.512Z",
    ],
)
def test_malformed_capture_time_is_rejected(value: str) -> None:
    assert only(validate_sample(mutated(ts=value))) == ("ts", "invalid_format")


# --- Run_Identifier ----------------------------------------------------------------

VALID_RUN_IDS = [
    "a", "A", "0", "a1", "manual-1a2b3c4d", "a-20250101T120000Z-9f3a1c2e", "c-20250101T121500Z-4be07a11",
    "a.b", "a_b", "a-b", "a.b_c-d", "r1", "x" * 64, "A" + "." * 63, "0" + "-" * 63,
]
INVALID_RUN_IDS = [
    "", "x" * 65, "x" * 200, "-abc", ".abc", "_abc", "_rejections", "_rejections.jsonl", "../x", "a/b", "a\\b", "a b", " a", "a ",
    "a\n", "abc\n", "\nabc", "a\tb", "é", "aé", "a\x00", "a:b", "a*b", "a?b", "a;b", "a,b", "a|b", "ａ", "a" + "\u2028",
]


@pytest.mark.parametrize("value", VALID_RUN_IDS)
def test_valid_run_ids(value: str) -> None:
    assert validate_run_id(value) is None
    assert validate_sample(mutated(run_id=value)) == []


@pytest.mark.parametrize("value", INVALID_RUN_IDS)
def test_invalid_run_ids(value: str) -> None:
    issue = validate_run_id(value)
    assert issue is not None
    assert (issue.field, issue.reason_code) == ("run_id", "invalid_run_id")
    assert only(validate_sample(mutated(run_id=value))) == ("run_id", "invalid_run_id")


def test_run_id_boundary_is_exactly_64_characters() -> None:
    assert validate_run_id("a" * 64) is None
    assert validate_run_id("a" * 65) is not None
    assert "65" in validate_run_id("a" * 65).reason  # type: ignore[union-attr]


def test_run_id_regex_alone_would_accept_a_trailing_newline_so_fullmatch_is_used() -> None:
    assert RUN_ID_RE.match("abc\n") is not None  # the approved pattern's `$` behaviour
    assert validate_run_id("abc\n") is not None  # validate_run_id is not fooled


@pytest.mark.parametrize("value", [None, 5, 1.5, True, [], {}, b"abc"])
def test_run_id_must_be_a_string(value: object) -> None:
    issue = validate_run_id(value)
    assert issue is not None and issue.reason_code == "invalid_run_id"


def test_run_id_issue_messages_do_not_echo_huge_values() -> None:
    issue = validate_run_id("x" * 5000 + "!")
    assert issue is not None and len(issue.reason) < 400


# --- container id and source -------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["", "abc", "0123456789a", "0123456789abc", HEX64.upper(), "0123456789AB", HEX64[:-1], HEX64 + "0", "g123456789ab",
     "0123456789ab\n", " 0123456789ab", "0123456789a b", "sha256:" + HEX64],
)
def test_malformed_container_id_is_rejected(value: str) -> None:
    assert only(validate_sample(mutated(container_id=value))) == ("container_id", "invalid_format")


@pytest.mark.parametrize("value", ["docker", "MOUNTINFO", "", "mount info", "proc"])
def test_container_id_source_must_be_in_the_allowed_set(value: str) -> None:
    assert only(validate_sample(mutated(container_id_source=value))) == ("container_id_source", "out_of_range")


# --- null needs a reason -----------------------------------------------------------


@pytest.mark.parametrize("field", NULLABLE)
def test_null_without_a_reason_is_rejected(field: str) -> None:
    assert only(validate_sample(mutated(**{field: None}))) == (field, "null_without_reason")


@pytest.mark.parametrize("field", NULLABLE)
def test_null_with_a_reason_is_valid(field: str) -> None:
    assert validate_sample(mutated(**{field: None, "null_reasons": {field: "cannot be read"}})) == []


def test_all_nullable_fields_null_with_reasons() -> None:
    reasons = {field: f"{field} unavailable" for field in NULLABLE}
    assert validate_sample(mutated(null_reasons=reasons, **{field: None for field in NULLABLE})) == []


def test_null_with_reasons_object_missing_that_key() -> None:
    sample = mutated(rss_bytes=None, request_count=None, null_reasons={"rss_bytes": "no /proc"})
    assert pairs(validate_sample(sample)) == [("request_count", "null_without_reason")]


def test_null_with_empty_reasons_object() -> None:
    assert only(validate_sample(mutated(rss_bytes=None, null_reasons={}))) == ("rss_bytes", "null_without_reason")


@pytest.mark.parametrize("reason, ok", [("x", True), ("x" * 200, True), ("", False), ("x" * 201, False), ("é" * 200, True), ("é" * 201, False)])
def test_reason_length_is_1_to_200_characters(reason: str, ok: bool) -> None:
    result = validate_sample(mutated(rss_bytes=None, null_reasons={"rss_bytes": reason}))
    if ok:
        assert result == []
    else:
        assert only(result) == ("null_reasons.rss_bytes", "out_of_range")


@pytest.mark.parametrize("reason", [5, None, True, ["x"], {"a": 1}, 1.5])
def test_reason_must_be_a_string(reason: object) -> None:
    assert only(validate_sample(mutated(rss_bytes=None, null_reasons={"rss_bytes": reason}))) == ("null_reasons.rss_bytes", "wrong_type")


@pytest.mark.parametrize("value", ["rss", [], None, 5, True])
def test_null_reasons_must_be_an_object(value: object) -> None:
    assert only(validate_sample(mutated(null_reasons=value))) == ("null_reasons", "wrong_type")


def test_a_reason_for_a_field_that_is_not_nullable_is_unknown() -> None:
    assert only(validate_sample(mutated(null_reasons={"seq": "why"}))) == ("null_reasons.seq", "unknown_field")
    assert only(validate_sample(mutated(null_reasons={"nonsense": "why"}))) == ("null_reasons.nonsense", "unknown_field")


def test_a_reason_for_a_field_that_is_not_null_is_rejected() -> None:
    assert only(validate_sample(mutated(null_reasons={"rss_bytes": "cannot read"}))) == ("null_reasons.rss_bytes", "invalid_format")


def test_empty_null_reasons_object_is_allowed_when_nothing_is_null() -> None:
    assert validate_sample(mutated(null_reasons={})) == []


# --- behaviour of the validators ---------------------------------------------------


def test_all_problems_are_reported_together_in_a_stable_order() -> None:
    sample = mutated(seq=-1, ts="bad", interval_ms=5, dropped=-1, extra=1, rss_bytes=None)
    first = pairs(validate_sample(sample))
    assert first == [
        ("seq", "out_of_range"), ("ts", "invalid_format"), ("rss_bytes", "null_without_reason"),
        ("interval_ms", "out_of_range"), ("dropped", "out_of_range"), ("extra", "unknown_field"),
    ]
    assert pairs(validate_sample(sample)) == first


def test_validators_do_not_modify_their_input() -> None:
    sample = mutated(seq=-1, extra=1, rss_bytes=None, null_reasons={"rss_bytes": "", "bogus": "x"})
    snapshot = copy.deepcopy(sample)
    validate_sample(sample)
    assert sample == snapshot


def test_issue_reasons_are_non_empty_text() -> None:
    for issue in validate_sample(mutated(seq=-1, extra=1, ts=5)):
        assert isinstance(issue.reason, str) and issue.reason


@pytest.mark.parametrize("junk", [object(), set(), (1, 2), b"{}", lambda: None])
def test_validators_never_raise_on_junk(junk: object) -> None:
    assert validate_sample(junk)
    assert validate_record(junk)


# --- validate_record: envelope and records -----------------------------------------


def stored() -> dict:
    return json.loads(STORED_SAMPLE_JSON)


def test_stored_sample_record_is_valid_and_keeps_both_times() -> None:
    record = stored()
    assert validate_record(record) == []
    assert record["sample"]["ts"] == "2025-01-01T12:00:06.512Z"  # Capture_Time, Probe clock
    assert record["recv_time"] == "2025-01-01T12:00:06.530Z"  # Receive_Time, Collector clock


@pytest.mark.parametrize("name", sorted(OTHER_RECORD_JSON))
def test_design_example_records_of_every_other_type_pass_the_envelope_check(name: str) -> None:
    assert validate_record(json.loads(OTHER_RECORD_JSON[name])) == []


def test_every_record_type_is_covered_by_an_example() -> None:
    seen = {json.loads(STORED_SAMPLE_JSON)["record_type"]} | {json.loads(t)["record_type"] for t in OTHER_RECORD_JSON.values()}
    assert seen == set(RECORD_TYPES)


@pytest.mark.parametrize("field", ["record_type", "schema_version", "recv_time"])
def test_envelope_field_missing(field: str) -> None:
    record = stored()
    del record[field]
    assert (field, "missing_field") in pairs(validate_record(record))


def test_envelope_run_id_missing() -> None:
    record = stored()
    del record["run_id"]
    assert pairs(validate_record(record))[0] == ("run_id", "invalid_run_id")


@pytest.mark.parametrize("value", [1, None, True, [], "Sample", "SAMPLE", "samples", ""])
def test_record_type_must_be_a_known_string(value: object) -> None:
    record = stored()
    record["record_type"] = value
    found = pairs(validate_record(record))
    code = "wrong_type" if not isinstance(value, str) else "out_of_range"
    assert ("record_type", code) in found


@pytest.mark.parametrize("value, code", [("1", "wrong_type"), (1.0, "wrong_type"), (True, "wrong_type"), (None, "wrong_type"), (0, "out_of_range"), (2, "out_of_range"), (-1, "out_of_range")])
def test_schema_version_must_be_the_integer_one(value: object, code: str) -> None:
    record = stored()
    record["schema_version"] = value
    assert only(validate_record(record)) == ("schema_version", code)


@pytest.mark.parametrize("value", ["", "2025-01-01T12:00:06Z", "2025-01-01T12:00:06.530+00:00", "now", "2025-13-01T00:00:00.000Z"])
def test_recv_time_must_be_a_utc_millisecond_timestamp(value: str) -> None:
    record = stored()
    record["recv_time"] = value
    assert only(validate_record(record)) == ("recv_time", "invalid_format")


@pytest.mark.parametrize("value", [5, None, True, 1.5, []])
def test_recv_time_of_the_wrong_type(value: object) -> None:
    record = stored()
    record["recv_time"] = value
    assert only(validate_record(record)) == ("recv_time", "wrong_type")


@pytest.mark.parametrize("value", ["", "x" * 65, "-a", "a/b", "abc\n", 5, True, []])
def test_envelope_run_id_must_be_valid(value: object) -> None:
    record = json.loads(OTHER_RECORD_JSON["repair"])
    record["run_id"] = value
    assert only(validate_record(record)) == ("run_id", "invalid_run_id")


def test_null_run_id_is_allowed_only_for_rejection_records() -> None:
    assert validate_record(json.loads(OTHER_RECORD_JSON["rejection_no_run"])) == []
    for name in ("repair", "run_metadata", "termination", "gap_silence"):
        record = json.loads(OTHER_RECORD_JSON[name])
        record["run_id"] = None
        assert only(validate_record(record)) == ("run_id", "invalid_run_id")
    record = stored()
    record["run_id"] = None
    assert ("run_id", "invalid_run_id") in pairs(validate_record(record))


@pytest.mark.parametrize("value", [None, [], "record", 5, True, 1.5])
def test_a_record_that_is_not_an_object(value: object) -> None:
    assert pairs(validate_record(value)) == [("$", "wrong_type")]


def test_empty_record_reports_the_whole_envelope() -> None:
    assert {f for f, _ in pairs(validate_record({}))} == {"record_type", "schema_version", "run_id", "recv_time"}


# --- validate_record: sample records -----------------------------------------------


def test_sample_record_without_the_nested_sample() -> None:
    record = stored()
    del record["sample"]
    assert only(validate_record(record)) == ("sample", "missing_field")


@pytest.mark.parametrize("value", [None, [], "s", 5])
def test_sample_record_with_a_nested_sample_that_is_not_an_object(value: object) -> None:
    record = stored()
    record["sample"] = value
    assert ("sample", "wrong_type") in pairs(validate_record(record))


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"seq": -1}, ("sample.seq", "out_of_range")),
        ({"ts": "bad"}, ("sample.ts", "invalid_format")),
        ({"rss_bytes": True}, ("sample.rss_bytes", "wrong_type")),
        ({"loop_delay_ms": float("nan")}, ("sample.loop_delay_ms", "out_of_range")),
        ({"interval_ms": 99}, ("sample.interval_ms", "out_of_range")),
        ({"extra": 1}, ("sample.extra", "unknown_field")),
        ({"rss_bytes": None}, ("sample.rss_bytes", "null_without_reason")),
    ],
)
def test_nested_sample_problems_are_reported_with_a_sample_prefix(changes: dict, expected: tuple[str, str]) -> None:
    record = stored()
    record["sample"].update(changes)
    assert only(validate_record(record)) == expected


def test_envelope_and_nested_run_ids_must_agree() -> None:
    record = stored()
    record["run_id"] = "other-run"
    assert only(validate_record(record)) == ("run_id", "invalid_run_id")


def test_sample_record_rejects_extra_top_level_fields() -> None:
    record = stored()
    record["note"] = "hello"
    assert only(validate_record(record)) == ("note", "unknown_field")


def test_other_record_types_may_carry_their_own_fields() -> None:
    record = json.loads(OTHER_RECORD_JSON["repair"])
    record["anything_extra"] = 1
    assert validate_record(record) == []


def test_validate_record_does_not_modify_its_input() -> None:
    record = stored()
    record["sample"]["seq"] = -5
    snapshot = copy.deepcopy(record)
    validate_record(record)
    assert record == snapshot


def test_stored_record_round_trips_through_compact_json() -> None:
    record = stored()
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    assert "\n" not in line
    assert json.loads(line) == record
    assert validate_record(json.loads(line)) == []

# =====================================================================================
# Added by task 2.3 (gaps found by the coverage audit)
# =====================================================================================


def test_tests_exercise_the_real_schema_module() -> None:
    for function in (validate_sample, validate_record, validate_run_id):
        assert function.__module__ == "coroner.evidence.schema"
    assert ValidationIssue.__module__ == "coroner.evidence.schema"
    assert schema.__name__ == "coroner.evidence.schema"
    assert schema.__file__ is not None and schema.__file__.replace("\\", "/").endswith("coroner/evidence/schema.py")


JUNK_VALUES = [None, True, False, "", "x", [], {}, 1.5, -1, 2**70, float("nan"), float("inf")]
# The only (field, value) pairs from JUNK_VALUES that are valid. Everything else must be rejected.
# pid has no range in the design (type only); the other three accept any non-negative integer;
# "x" is a valid Run_Identifier.
VALID_JUNK = {
    ("run_id", repr("x")),
    ("seq", repr(2**70)),
    ("rss_bytes", repr(2**70)),
    ("request_count", repr(2**70)),
    ("loop_delay_ms", repr(1.5)),
    ("loop_delay_ms", repr(2**70)),
    ("pid", repr(-1)),
    ("pid", repr(2**70)),
}


@pytest.mark.parametrize("field", REQUIRED)
def test_every_junk_replacement_of_every_field_is_rejected_unless_it_is_valid(field: str) -> None:
    """Sweep: each mutation is rejected, and the first issue names the mutated field."""
    for junk in JUNK_VALUES:
        issues = validate_sample(mutated(**{field: junk}))
        if (field, repr(junk)) in VALID_JUNK:
            assert issues == [], (field, junk, issues)
            continue
        assert issues, (field, junk)
        assert issues[0].field == field, (field, junk, issues)
        assert all(i.reason_code in schema.REASON_CODES for i in issues)


def test_every_reason_code_is_produced_by_some_mutation() -> None:
    produced: set[str] = set()
    cases = [
        validate_sample(mutated(run_id="")), validate_sample(without("seq")), validate_sample(mutated(seq="1")),
        validate_sample(mutated(seq=-1)), validate_sample(mutated(ts="bad")), validate_sample(mutated(rss_bytes=None)),
        validate_sample(mutated(extra=1)), validate_record({}),
    ]
    for issues in cases:
        produced |= {i.reason_code for i in issues}
    assert produced == set(schema.REASON_CODES)


def test_issue_fields_are_wellformed_paths_and_reason_codes_are_known() -> None:
    import re

    path = re.compile(r"\$|[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}")
    bad_sample = mutated(seq=-1, ts=5, extra=1, rss_bytes=None, null_reasons={"seq": "x", "request_count": ""})
    record = stored()
    record["sample"] = bad_sample
    record["schema_version"] = 9
    record["note"] = 1
    for issue in validate_sample(bad_sample) + validate_record(record) + validate_sample(7) + validate_record(7):
        assert path.fullmatch(issue.field), issue
        assert issue.reason_code in schema.REASON_CODES, issue
        assert issue.reason


def test_nested_sample_missing_capture_time() -> None:
    record = stored()
    del record["sample"]["ts"]
    assert only(validate_record(record)) == ("sample.ts", "missing_field")


def test_capture_time_and_receive_time_are_independent_values() -> None:
    """Experiment B: a Sample captured before a kill may be received after it, and clocks may differ."""
    later_capture = stored()
    later_capture["recv_time"] = "2025-01-01T12:00:00.000Z"  # earlier than sample.ts (12:00:06.512Z)
    assert validate_record(later_capture) == []
    same = stored()
    same["recv_time"] = same["sample"]["ts"]
    assert validate_record(same) == []
    # Neither time can stand in for the other.
    no_capture = stored()
    del no_capture["sample"]["ts"]
    no_receive = stored()
    del no_receive["recv_time"]
    assert pairs(validate_record(no_capture)) == [("sample.ts", "missing_field")]
    assert pairs(validate_record(no_receive)) == [("recv_time", "missing_field")]


def test_the_same_input_always_gives_the_same_issues() -> None:
    sample = mutated(seq=-1, ts="bad", extra=1, rss_bytes=None, null_reasons={"seq": "x"})
    results = [validate_sample(copy.deepcopy(sample)) for _ in range(5)]
    assert all(result == results[0] for result in results)
    record = stored()
    record["sample"] = sample
    again = [validate_record(copy.deepcopy(record)) for _ in range(3)]
    assert all(result == again[0] for result in again)


def _permuted(mapping: dict, rng) -> dict:
    items = list(mapping.items())
    rng.shuffle(items)
    return dict(items)


def test_issue_order_is_exactly_specified_and_independent_of_insertion_order() -> None:
    sample = mutated(
        zeta=1, alpha=2, rss_bytes=None, request_count=None, seq=-1,
        null_reasons={"seq": "x", "nonsense": "y", "request_count": "", "rss_bytes": "ok"},
    )
    expected = [
        ("seq", "out_of_range"),  # schema field order first
        ("null_reasons.nonsense", "unknown_field"),  # then null_reasons problems, sorted by key
        ("null_reasons.request_count", "out_of_range"),
        ("null_reasons.seq", "unknown_field"),
        ("alpha", "unknown_field"),  # then unknown fields, sorted by key
        ("zeta", "unknown_field"),
    ]
    assert pairs(validate_sample(sample)) == expected


def test_permuting_key_insertion_order_never_changes_the_complete_ordered_result_of_validate_sample() -> None:
    import itertools
    import random

    rng = random.Random(20241114)
    base = mutated(
        seq=-1, ts="bad", rss_bytes=None, request_count=None, interval_ms=5,
        e=1, d=2, c=3, b=4,
        null_reasons={"seq": "x", "nonsense": "y", "request_count": "", "container_id": "z", "rss_bytes": "ok"},
    )
    reference = validate_sample(base)
    assert len(reference) >= 10
    # Exhaustive over every order of the unknown fields' own keys ...
    unknown = {k: base[k] for k in ("e", "d", "c", "b")}
    rest = {k: v for k, v in base.items() if k not in unknown}
    for order in itertools.permutations(unknown):
        candidate = dict(rest)
        candidate.update({k: unknown[k] for k in order})
        assert validate_sample(candidate) == reference
    # ... exhaustive over every order of the null_reasons keys ...
    reasons = base["null_reasons"]
    for order in itertools.permutations(reasons):
        candidate = dict(base, null_reasons={k: reasons[k] for k in order})
        assert validate_sample(candidate) == reference
    # ... and many random orders of every key at once, including the reversed order.
    reverse = dict(reversed(list(base.items())))
    assert validate_sample(reverse) == reference
    for _ in range(300):
        candidate = _permuted(base, rng)
        candidate["null_reasons"] = _permuted(candidate["null_reasons"], rng)
        assert validate_sample(candidate) == reference


def test_permuting_key_insertion_order_never_changes_the_complete_ordered_result_of_validate_record() -> None:
    import random

    rng = random.Random(7)
    record = stored()
    record["sample"].update(seq=-1, rss_bytes=None, z=1, a=2, null_reasons={"seq": "x", "request_count": ""})
    record.update(note=1, extra=2, schema_version=9)
    reference = validate_record(record)
    assert len(reference) >= 6
    assert reference == validate_record(dict(reversed(list(record.items()))))
    for _ in range(300):
        candidate = _permuted(record, rng)
        candidate["sample"] = _permuted(candidate["sample"], rng)
        candidate["sample"]["null_reasons"] = _permuted(candidate["sample"]["null_reasons"], rng)
        assert validate_record(candidate) == reference


def test_unknown_top_level_fields_of_a_sample_record_are_reported_in_sorted_order() -> None:
    record = stored()
    record.update(zz=1, aa=2)
    assert pairs(validate_record(record)) == [("aa", "unknown_field"), ("zz", "unknown_field")]
    assert pairs(validate_record(dict(reversed(list(record.items()))))) == [("aa", "unknown_field"), ("zz", "unknown_field")]


def test_non_string_keys_never_make_validation_raise_and_are_ordered_deterministically() -> None:
    import itertools

    keys = [1, (1, 2), None, 2.5, "b", "a", True]
    results = []
    for order in itertools.islice(itertools.permutations(keys), 200):
        sample = good()
        sample.update({key: 0 for key in order})
        results.append(validate_sample(sample))
    assert all(result == results[0] for result in results)
    assert {i.reason_code for i in results[0]} == {"unknown_field"}
    fields = [i.field for i in results[0]]
    assert fields[:2] == ["a", "b"]  # strings first, in code-point order
    record = stored()
    record.update({1: "x", "k": "y"})
    assert pairs(validate_record(record))[:2] == [("k", "unknown_field"), ("1", "unknown_field")]
    reasons = mutated(rss_bytes=None, null_reasons={3: "x", "rss_bytes": "ok"})
    assert pairs(validate_sample(reasons)) == [("null_reasons.3", "unknown_field")]

def test_dict_subclasses_are_treated_as_objects() -> None:
    from collections import OrderedDict

    assert validate_sample(OrderedDict(good())) == []
    assert validate_record(OrderedDict(stored())) == []


def test_requirement_7_7_rss_and_loop_delay_missing_non_numeric_or_null_without_reason() -> None:
    for field in ("rss_bytes", "loop_delay_ms"):
        assert only(validate_sample(without(field))) == (field, "missing_field")
        assert only(validate_sample(mutated(**{field: "12"}))) == (field, "wrong_type")
        assert only(validate_sample(mutated(**{field: None}))) == (field, "null_without_reason")
        assert validate_sample(mutated(**{field: None, "null_reasons": {field: "unreadable"}})) == []


def test_requirement_4_9_run_id_missing_empty_or_too_long() -> None:
    for sample in (without("run_id"), mutated(run_id=""), mutated(run_id="r" * 65)):
        assert only(validate_sample(sample)) == ("run_id", "invalid_run_id")