"""Property 4: Invalid input is rejected and never stored as a Sample (Task 3.7).

**Validates: Requirements 4.5, 4.9, 7.7**

Contract tested through the real `IngestService` (real files in a temporary directory, injected clock and fsync
doubles). A schema-valid Sample gets EXACTLY ONE invalid mutation, drawn from these categories (the numbers are
the task's; 21 is an extra for the schema's string-format and enumeration rules):

     1 invalid UTF-8 bytes              2 unparseable JSON               3 truncated JSON
     4 duplicate JSON keys (D33)        5 missing required field         6 wrong field type
     7 missing run_id                   8 empty run_id                   9 run_id longer than 64 characters
    10 run_id with invalid characters  11 missing rss_bytes             12 invalid rss_bytes type
    13 null rss_bytes without reason   14 missing loop_delay_ms         15 invalid loop_delay_ms type
    16 null loop_delay_ms without reason  17 out-of-range numbers       18 unknown fields
    19 NaN / Infinity / -Infinity literals  20 invalid null_reasons content  21 bad ts / container_id / source

Independent oracle. The expected status (422), reason code, field and routing are fixed by the mutation's
construction and re-derived here from the mutated JSON by `defects_of`, a small re-implementation of the schema
rules written for this test. `validate_sample` is never used to decide the expectation; it is called afterwards
only as a cross-check. The expected rejection line is built from the input and the injected clock, never from
what the Collector stored.

Expected results (reason code; field; where the rejection goes)
    1, 2, 3, 4, 19   invalid_json; ""; `_rejections.jsonl` with run_id null (the body is not a trusted object)
    7, 8, 9, 10      invalid_run_id; "run_id"; `_rejections.jsonl` with run_id null (also a wrong-type run_id in 6)
    5, 11, 14        missing_field; the field; the run's file
    6, 12, 15        wrong_type; the field; the run's file
    13, 16           null_without_reason; the field; the run's file
    17               out_of_range; the field; the run's file
    18               unknown_field; the first unknown key in code point order; the run's file
    20               wrong_type / out_of_range / unknown_field / invalid_format; `null_reasons...`; the run's file
    21               invalid_format (ts, container_id) or out_of_range (container_id_source); the run's file
"""
from __future__ import annotations

import base64
import json
import math
import re
import string
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from coroner.collector import service as service_module
from coroner.collector.config import CollectorConfig
from coroner.collector.service import IngestService, create_ingest_service
from coroner.common.timeutil import parse_utc_ms
from coroner.evidence.reader import read_evidence
from coroner.evidence.schema import validate_record, validate_sample
from coroner.evidence.writer import Durability
from tests.unit.collector.test_prop_ingest_stored import base_sample, line_of
from tests.unit.collector.test_prop_ingest_stored import cases as base_cases
from tests.unit.collector.test_service import FakeProbe, Faults, Rec

MAX_BODY = 65536
NOW = "2031-02-03T04:05:06.789Z"
EARLIER = "2030-01-01T00:00:00.000Z"
PREVIEW_CAP = 1024

ALNUM = string.ascii_letters + string.digits
RUN_CHARS = ALNUM + "._-"
REQUIRED_F = ("run_id", "seq", "ts", "rss_bytes", "request_count", "loop_delay_ms", "pid", "container_id", "container_id_source", "interval_ms", "dropped")
NULLABLE_F = ("rss_bytes", "request_count", "loop_delay_ms", "container_id")
ALLOWED_KEYS = frozenset(REQUIRED_F) | {"null_reasons"}
SOURCES = ("mountinfo", "cgroup", "hostname", "none")
VALID_VALUE = {"rss_bytes": 1234, "request_count": 5, "loop_delay_ms": 0.5, "container_id": "3f1c0e0a9d7b"}
ALL_CATEGORIES = tuple(range(1, 22))
CATEGORY_NAMES = {
    1: "invalid UTF-8", 2: "unparseable JSON", 3: "truncated JSON", 4: "duplicate JSON keys", 5: "missing required field",
    6: "wrong field type", 7: "missing run_id", 8: "empty run_id", 9: "run_id over 64 characters", 10: "run_id with invalid characters",
    11: "missing rss_bytes", 12: "invalid rss_bytes type", 13: "null rss_bytes without reason", 14: "missing loop_delay_ms",
    15: "invalid loop_delay_ms type", 16: "null loop_delay_ms without reason", 17: "out-of-range numbers", 18: "unknown fields",
    19: "NaN / Infinity literals", 20: "invalid null_reasons content", 21: "bad ts / container_id / container_id_source",
}
_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_CONTAINER_RE = re.compile(r"(?:[0-9a-f]{12}|[0-9a-f]{64})")


# --- the independent oracle: what the schema says about an already parsed object ------------------------------------


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_number(value: object) -> bool:
    return type(value) in (int, float)


def own_run_id_ok(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64 and value[0] in ALNUM and all(ch in RUN_CHARS for ch in value)


def own_ts_ok(value: object) -> bool:
    if not isinstance(value, str) or _TS_RE.fullmatch(value) is None:
        return False
    try:
        datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]), int(value[11:13]), int(value[14:16]), int(value[17:19]))
    except ValueError:
        return False
    return True


def value_defect(name: str, value: object) -> str | None:
    """The reason code for one non-null Sample field value, or None when the value is fine."""
    if name == "run_id":
        return None if own_run_id_ok(value) else "invalid_run_id"
    if name in ("seq", "rss_bytes", "request_count", "interval_ms", "dropped", "pid"):
        if not _is_int(value):
            return "wrong_type"
        low, high = {"interval_ms": (100, 60000), "dropped": (0, 2**32 - 1), "pid": (None, None)}.get(name, (0, None))
        assert isinstance(value, int)
        if (low is not None and value < low) or (high is not None and value > high):
            return "out_of_range"
        return None
    if name == "ts":
        if not isinstance(value, str):
            return "wrong_type"
        return None if own_ts_ok(value) else "invalid_format"
    if name == "loop_delay_ms":
        if not _is_number(value):
            return "wrong_type"
        assert isinstance(value, (int, float))
        return "out_of_range" if (isinstance(value, float) and not math.isfinite(value)) or value < 0 else None
    if name == "container_id":
        if not isinstance(value, str):
            return "wrong_type"
        return None if _CONTAINER_RE.fullmatch(value) else "invalid_format"
    if name == "container_id_source":
        if not isinstance(value, str):
            return "wrong_type"
        return None if value in SOURCES else "out_of_range"
    raise AssertionError(name)


def defects_of(obj: object) -> list[tuple[str, str]]:
    """(reason_code, field) for every problem, in the order the schema reports them (required fields, null_reasons, unknown keys)."""
    if not isinstance(obj, dict):
        return [("wrong_type", "$")]
    reason_defects: list[tuple[str, str]] = []
    usable: set[str] = set()
    if "null_reasons" in obj:
        reasons = obj["null_reasons"]
        if not isinstance(reasons, dict):
            reason_defects.append(("wrong_type", "null_reasons"))
        else:
            for key in sorted(reasons):
                value, path = reasons[key], f"null_reasons.{key}"
                if key not in NULLABLE_F:
                    reason_defects.append(("unknown_field", path))
                elif not isinstance(value, str):
                    reason_defects.append(("wrong_type", path))
                elif not 1 <= len(value) <= 200:
                    reason_defects.append(("out_of_range", path))
                elif obj.get(key) is not None:
                    reason_defects.append(("invalid_format", path))
                else:
                    usable.add(key)
    bad_paths = {path for _, path in reason_defects}
    out: list[tuple[str, str]] = []
    for name in REQUIRED_F:
        if name not in obj:
            out.append(("invalid_run_id" if name == "run_id" else "missing_field", name))
            continue
        value = obj[name]
        if value is None and name in NULLABLE_F:
            if name not in usable and f"null_reasons.{name}" not in bad_paths:
                out.append(("null_without_reason", name))
            continue
        code = value_defect(name, value)
        if code is not None:
            out.append((code, name))
    out.extend(reason_defects)
    out.extend(("unknown_field", key) for key in sorted(obj) if key not in ALLOWED_KEYS)
    return out


def top_diff(base: dict[str, Any], parsed: dict[str, Any]) -> set[str]:
    """Top-level keys that were removed, added or changed. The comparison is by JSON value, `1` and `1.0` differ."""

    def same(a: object, b: object) -> bool:
        return type(a) is type(b) and (a == b or (isinstance(a, float) and math.isnan(a) and math.isnan(b)))  # type: ignore[arg-type]

    keys = set(base) | set(parsed)
    return {k for k in keys if k not in base or k not in parsed or not same(base[k], parsed[k])}


def count_duplicate_keys(text: str) -> int:
    found = 0

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal found
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                found += 1
            out[key] = value
        return out

    json.loads(text, object_pairs_hook=hook)
    return found


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


# --- mutation records and structural checks ------------------------------------------------------------------------


@dataclass
class Style:
    ensure_ascii: bool
    indent: int | None
    separators: tuple[str, str] | None
    pad_l: str
    pad_r: str


@dataclass
class Mutation:
    category: int
    raw: bytes
    code: str  # expected reason code
    field: str  # expected field ("" for invalid_json)
    route: str | None  # the run whose file gets the rejection, or None for `_rejections.jsonl`
    base: dict[str, Any]  # the schema-valid Sample the mutation started from
    durability: Durability = Durability.CRASH
    info: dict[str, Any] = field(default_factory=dict)


def must_fail(call: Callable[[], object], error: type[Exception]) -> None:
    """AssertionError (not pytest's Failed) unless `call` raises `error`, so the structural check can itself be tested."""
    try:
        call()
    except error:
        return
    raise AssertionError(f"expected {error.__name__}")


def check_structure(m: Mutation) -> None:
    """Raise AssertionError unless the mutated body is in its intended category and has no other invalidity."""
    cat, raw = m.category, m.raw
    assert len(raw) <= MAX_BODY, f"body is {len(raw)} bytes"
    if cat == 1:
        must_fail(lambda: raw.decode("utf-8"), UnicodeDecodeError)
        assert (m.code, m.field, m.route) == ("invalid_json", "", None)
        return
    text = raw.decode("utf-8")
    if cat in (2, 3):
        must_fail(lambda: json.loads(text), ValueError)
        if cat == 3:
            compact, seen = m.info["compact"], text.lstrip(" \t\r\n")
            assert compact.startswith(seen) and len(seen) < len(compact), "not a strict prefix of the valid document"
        assert (m.code, m.field, m.route) == ("invalid_json", "", None)
        return
    if cat == 4:
        json.loads(text)  # a default parser accepts it (the last value wins), so only the duplicate makes it invalid
        assert count_duplicate_keys(text) == 1
        assert (m.code, m.field, m.route) == ("invalid_json", "", None)
        return
    parsed = json.loads(text)
    assert isinstance(parsed, dict)
    if cat == 19:
        must_fail(lambda: json.loads(text, parse_constant=_reject_constant), ValueError)
        name = m.info["field"]
        assert isinstance(parsed[name], float) and not math.isfinite(parsed[name])
        assert {k: v for k, v in parsed.items() if k != name} == {k: v for k, v in m.base.items() if k != name}
        assert (m.code, m.field, m.route) == ("invalid_json", "", None)
        return
    defects = defects_of(parsed)
    assert defects, "the mutation is not invalid at all"
    assert defects[0] == (m.code, m.field), f"expected {(m.code, m.field)}, derived {defects}"
    if cat == 18:
        assert len(defects) == len(m.info["unknown"]) and {c for c, _ in defects} == {"unknown_field"}
    else:
        assert len(defects) == 1, f"more than one invalidity: {defects}"
    allowed = set(m.info.get("touched", ())) | {"null_reasons"}
    if cat == 20:
        allowed |= set(NULLABLE_F)
    assert top_diff(m.base, parsed) <= allowed, f"unrelated change: {top_diff(m.base, parsed) - allowed}"
    route = parsed["run_id"] if own_run_id_ok(parsed.get("run_id")) else None
    assert m.route == route, f"route {m.route!r} but the run_id says {route!r}"
    rid = parsed.get("run_id")
    if cat == 5:
        assert m.field not in ("run_id", "rss_bytes", "loop_delay_ms") and m.field not in parsed
    elif cat == 7:
        assert "run_id" not in parsed
    elif cat == 8:
        assert rid == ""
    elif cat == 9:
        assert isinstance(rid, str) and len(rid) > 64 and rid[0] in ALNUM and all(ch in RUN_CHARS for ch in rid)
    elif cat == 10:
        assert isinstance(rid, str) and 1 <= len(rid) <= 64
    elif cat in (11, 14):
        assert m.field == ("rss_bytes" if cat == 11 else "loop_delay_ms") and m.field not in parsed
    elif cat in (12, 15, 13, 16):
        assert m.field == ("rss_bytes" if cat in (12, 13) else "loop_delay_ms")
        if cat in (13, 16):
            assert parsed[m.field] is None and m.field not in parsed.get("null_reasons", {})
        else:
            assert m.code == "wrong_type" and parsed[m.field] is not None
    elif cat == 17:
        assert m.code == "out_of_range" and _is_number(parsed[m.field])
    elif cat == 21:
        assert m.code in ("invalid_format", "out_of_range") and isinstance(parsed[m.field], str)


# --- builders: one mutation per call ------------------------------------------------------------------------------


def text_of(obj: dict[str, Any], style: Style, raws: dict[str, str] | None = None) -> str:
    """The document: padding, `json.dumps`, padding. `raws` swaps a unique string token for a literal that json cannot dump."""
    text = json.dumps(obj, ensure_ascii=style.ensure_ascii, indent=style.indent, separators=style.separators)
    for token, literal in (raws or {}).items():
        needle = json.dumps(token, ensure_ascii=style.ensure_ascii)
        assert text.count(needle) == 1, token
        text = text.replace(needle, literal)
    return style.pad_l + text + style.pad_r


def make_null(obj: dict[str, Any], name: str, reason: str) -> dict[str, Any]:
    out = dict(obj)
    out[name] = None
    reasons = dict(out.get("null_reasons") or {})
    reasons[name] = reason
    out["null_reasons"] = reasons
    return out


def make_value(obj: dict[str, Any], name: str, value: Any) -> dict[str, Any]:
    """Set `name` and drop its null reason, so the value is the only thing that is wrong with the Sample."""
    out = dict(obj)
    out[name] = value
    if isinstance(out.get("null_reasons"), dict):
        out["null_reasons"] = {k: v for k, v in out["null_reasons"].items() if k != name}
    return out


def schema_case(cat: int, mutated: dict[str, Any], style: Style, base: dict[str, Any], code: str, name: str, touched: set[str],
                raws: dict[str, str] | None = None, **info: Any) -> Mutation:
    route = mutated["run_id"] if own_run_id_ok(mutated.get("run_id")) else None
    info["touched"] = touched
    return Mutation(cat, text_of(mutated, style, raws).encode("utf-8"), code, name, route, base, info=info)


def parse_case(cat: int, raw: bytes, base: dict[str, Any], **info: Any) -> Mutation:
    return Mutation(cat, raw, "invalid_json", "", None, base, info=info)


ok_reason = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=200)
INVALID_UTF8 = [b"\xff", b"\xfe", b"\xc0\xaf", b"\xc1\xbf", b"\xed\xa0\x80", b"\xf5\x80\x80\x80", b"\xf8\x88\x80\x80\x80", b"\xff\xfe"]
GARBAGE = ["not json", "hello", "<xml/>", "{'a': 1}", "{a:1}", "undefined", "NULL", "tru", "{,}", '{"a" 1}', '{"a":}', "}", "\x00", '{"a":1}}']
SUFFIXES = ["x", "}", "]", ",", "{}", "1", '"a"', "//c", "\x00", ";"]
PREFIXES = ["\ufeff", "x", "(", ")", ";"]


def b1(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    data = text_of(obj, s).encode("utf-8")
    if draw(st.booleans()):
        junk = draw(st.sampled_from(INVALID_UTF8))
        pos = draw(st.integers(0, len(data)))
        raw = data[:pos] + junk + data[pos:]
    else:
        raw = data + draw(st.sampled_from([b"\xe2\x82", b"\xf0\x9f\x98", b"\xc3"]))  # a multi-byte sequence cut off at the end
    return parse_case(1, raw, obj)


def b2(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    text = text_of(obj, s)
    kind = draw(st.sampled_from(["garbage", "suffix", "prefix", "trailing_comma", "single_quotes", "concat", "blank"]))
    if kind == "garbage":
        text = draw(st.sampled_from(GARBAGE))
    elif kind == "suffix":
        text = text.rstrip() + draw(st.sampled_from(SUFFIXES))
    elif kind == "prefix":
        text = draw(st.sampled_from(PREFIXES)) + text
    elif kind == "trailing_comma":
        text = text.rstrip()[:-1].rstrip() + ",}"
    elif kind == "single_quotes":
        text = text.replace('"', "'")
    elif kind == "concat":
        text = text.strip() + text.strip()
    else:
        text = draw(st.sampled_from(["", " ", "\n\t "]))
    return parse_case(2, text.encode("utf-8"), obj, kind=kind)


def b3(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    compact = json.dumps(obj, ensure_ascii=s.ensure_ascii, indent=s.indent, separators=s.separators)
    cut = draw(st.integers(0, len(compact) - 1))
    raw = (s.pad_l + compact[:cut]).encode("utf-8")
    return parse_case(3, raw, obj, compact=compact)


_OTHER_VALUES = [None, 0, 1, "x", [], {}, True, 1.5]


def b4(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    nested = draw(st.booleans())
    work = obj
    if nested and not obj.get("null_reasons"):
        work = make_null(obj, draw(st.sampled_from(NULLABLE_F)), draw(ok_reason))
    item_sep, key_sep = s.separators if s.separators else (", ", ": ")

    def enc(value: object) -> str:
        return json.dumps(value, ensure_ascii=s.ensure_ascii)

    def joined(pairs: list[tuple[str, str]]) -> str:
        return "{" + item_sep.join(k + key_sep + v for k, v in pairs) + "}"

    def spelling(key: str) -> str:  # the same key, either as written or with every character escaped
        return enc(key) if draw(st.booleans()) else '"' + "".join(f"\\u{ord(ch):04x}" for ch in key) + '"'

    def dup_text(original: object) -> str:
        return enc(original) if draw(st.booleans()) else enc(draw(st.sampled_from(_OTHER_VALUES)))

    if nested:
        reasons = work["null_reasons"]
        key = draw(st.sampled_from(list(reasons)))
        target = [(enc(k), enc(v)) for k, v in reasons.items()]
        target.insert(draw(st.integers(0, len(target))), (spelling(key), dup_text(reasons[key])))
        top = [(enc(k), joined(target) if k == "null_reasons" else enc(v)) for k, v in work.items()]
    else:
        key = draw(st.sampled_from(list(work)))
        top = [(enc(k), enc(v)) for k, v in work.items()]
        top.insert(draw(st.integers(0, len(top))), (spelling(key), dup_text(work[key])))
    raw = (s.pad_l + joined(top) + s.pad_r).encode("utf-8")
    return parse_case(4, raw, obj, key=key, nested=nested)


def b5(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    name = draw(st.sampled_from(["seq", "ts", "request_count", "pid", "container_id", "container_id_source", "interval_ms", "dropped"]))
    return schema_case(5, {k: v for k, v in obj.items() if k != name}, s, obj, "missing_field", name, {name})


_NOT_INT = ["1", "", "x", True, False, [], {}, [1], {"a": 1}]
WRONG_VALUES: dict[str, list[Any]] = {
    "ts": [5, 1.5, True, False, None, [], {}, ["x"]],
    "loop_delay_ms": ["5", "", True, False, [], {}, [1]],
    "container_id": [5, 1.5, True, False, [], {}, ["a"]],
    "container_id_source": [5, 1.5, True, None, [], {}, ["mountinfo"]],
    "run_id": [5, 1.5, True, False, None, [], {}, ["r"]],
}
INT_FIELDS = ("seq", "rss_bytes", "request_count", "pid", "interval_ms", "dropped")


def wrong_type_value(draw: Callable[[Any], Any], name: str) -> Any:
    if name in WRONG_VALUES:
        return draw(st.sampled_from(WRONG_VALUES[name]))
    pool = _NOT_INT + ([] if name in NULLABLE_F else [None])
    return draw(st.one_of(st.sampled_from(pool), st.floats(allow_nan=False, allow_infinity=False)))


def wrong_type_case(draw: Callable[[Any], Any], cat: int, obj: dict[str, Any], s: Style, name: str) -> Mutation:
    value = wrong_type_value(draw, name)
    code = "invalid_run_id" if name == "run_id" else "wrong_type"
    return schema_case(cat, make_value(obj, name, value), s, obj, code, name, {name})


def b6(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    name = draw(st.sampled_from([n for n in REQUIRED_F if n not in ("rss_bytes", "loop_delay_ms")]))
    return wrong_type_case(draw, 6, obj, s, name)


def b7(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    return schema_case(7, {k: v for k, v in obj.items() if k != "run_id"}, s, obj, "invalid_run_id", "run_id", {"run_id"})


def b8(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    return schema_case(8, make_value(obj, "run_id", ""), s, obj, "invalid_run_id", "run_id", {"run_id"})


def b9(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    length = draw(st.one_of(st.sampled_from([65, 66, 100, 256]), st.integers(65, 400)))
    rid = draw(st.sampled_from(ALNUM)) + draw(st.text(alphabet=RUN_CHARS, min_size=length - 1, max_size=length - 1))
    return schema_case(9, make_value(obj, "run_id", rid), s, obj, "invalid_run_id", "run_id", {"run_id"})


BAD_CHARS = [" ", "/", "\\", ":", "*", "?", '"', "<", ">", "|", "\u00e9", "\x00", "\n", "\t", "\r", "\u202e", "\U0001f600", "\u00a0", "\u0663",
             "\uff5a", "\x7f", "%", "@", "$", "+", "=", "~", ",", "!", "'", "#", "&", "(", ")", "[", "]", "{", "}", ";", "^", "`", "\u2028"]
FIXED_BAD_RUN_IDS = ["_rejections", ".", "..", "-", "_", ".hidden", "-x", "_x", " ", "a b", "a/b", "../x", "x\n"]


def b10(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    if draw(st.booleans()):
        rid = draw(st.sampled_from(FIXED_BAD_RUN_IDS))
    else:
        head = draw(st.text(alphabet=RUN_CHARS, min_size=0, max_size=40))
        pos = draw(st.integers(0, len(head)))
        rid = head[:pos] + draw(st.sampled_from(BAD_CHARS)) + head[pos:]
    return schema_case(10, make_value(obj, "run_id", rid), s, obj, "invalid_run_id", "run_id", {"run_id"})


def b11(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    return schema_case(11, {k: v for k, v in obj.items() if k != "rss_bytes"}, s, obj, "missing_field", "rss_bytes", {"rss_bytes"})


def b12(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    return wrong_type_case(draw, 12, obj, s, "rss_bytes")


def null_without_reason(cat: int, name: str, obj: dict[str, Any], s: Style) -> Mutation:
    out = dict(obj)
    out[name] = None
    if isinstance(out.get("null_reasons"), dict):
        out["null_reasons"] = {k: v for k, v in out["null_reasons"].items() if k != name}
    return schema_case(cat, out, s, obj, "null_without_reason", name, {name})


def b13(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    return null_without_reason(13, "rss_bytes", obj, s)


def b14(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    return schema_case(14, {k: v for k, v in obj.items() if k != "loop_delay_ms"}, s, obj, "missing_field", "loop_delay_ms", {"loop_delay_ms"})


def b15(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    return wrong_type_case(draw, 15, obj, s, "loop_delay_ms")


def b16(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    return null_without_reason(16, "loop_delay_ms", obj, s)


def b17(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    name = draw(st.sampled_from(["seq", "rss_bytes", "request_count", "loop_delay_ms", "interval_ms", "dropped"]))
    raws: dict[str, str] | None = None
    if name in ("seq", "rss_bytes", "request_count"):
        value: Any = draw(st.one_of(st.sampled_from([-1, -2, -(2**31), -(2**63), -(2**64)]), st.integers(max_value=-1)))
    elif name == "loop_delay_ms":
        pick = draw(st.sampled_from(["negative", "negative_float", "huge", "neg_huge"]))
        if pick == "negative":
            value = draw(st.sampled_from([-1, -5, -(10**9)]))
        elif pick == "negative_float":
            value = draw(st.one_of(st.sampled_from([-0.5, -1e-9, -1e300]), st.floats(max_value=-1e-12, allow_nan=False, allow_infinity=False)))
        else:  # 1e999 is a legal JSON number that Python reads as infinity: valid JSON, out of range
            value, raws = "@@RAW0@@", {"@@RAW0@@": "1e999" if pick == "huge" else "-1e999"}
    elif name == "interval_ms":
        value = draw(st.one_of(st.sampled_from([99, 0, -1, 60001, 2**31, 2**64, -100]), st.integers(max_value=99), st.integers(min_value=60001)))
    else:
        value = draw(st.one_of(st.sampled_from([-1, 2**32, 2**40, 2**64]), st.integers(max_value=-1), st.integers(min_value=2**32)))
    return schema_case(17, make_value(obj, name, value), s, obj, "out_of_range", name, {name}, raws)


def b18(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    key = st.one_of(
        st.sampled_from(["extra", "Seq", "seq ", "run_id2", "", "__proto__", "null_reason", "unknown", "\x00", "\u2028", "pid\n"]),
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=30),
    ).filter(lambda k: k not in ALLOWED_KEYS)
    value = st.one_of(
        st.none(), st.booleans(), st.integers(), st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=50),
        st.just([]), st.just({"a": [1, 2]}), st.text(alphabet="abc xyz", min_size=1100, max_size=2500),
    )
    keys = draw(st.lists(key, min_size=1, max_size=3, unique=True))
    items = list(obj.items())
    for k in keys:
        items.insert(draw(st.integers(0, len(items))), (k, draw(value)))
    return schema_case(18, dict(items), s, obj, "unknown_field", min(keys), set(keys), unknown=keys)


def b19(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    name = draw(st.sampled_from(["seq", "rss_bytes", "request_count", "loop_delay_ms", "pid", "interval_ms", "dropped"]))
    mutated = dict(obj)
    mutated[name] = "@@RAW0@@"
    raw = text_of(mutated, s, {"@@RAW0@@": draw(st.sampled_from(["NaN", "Infinity", "-Infinity"]))}).encode("utf-8")
    return parse_case(19, raw, obj, field=name)


def b20(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    sub = draw(st.sampled_from(["wrong_type", "empty", "too_long", "non_nullable", "non_null_field", "unknown_key", "not_object"]))
    nullable = draw(st.sampled_from(NULLABLE_F))
    if sub in ("wrong_type", "empty", "too_long"):
        out = make_null(obj, nullable, draw(ok_reason))
        if sub == "wrong_type":
            bad: Any = draw(st.sampled_from([5, 1.5, True, False, None, [], {}, ["x"]]))
            code = "wrong_type"
        elif sub == "empty":
            bad, code = "", "out_of_range"
        else:
            length = draw(st.one_of(st.just(201), st.integers(201, 800)))
            bad = draw(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=length, max_size=length))
            code = "out_of_range"
        out["null_reasons"][nullable] = bad
        name = f"null_reasons.{nullable}"
    elif sub == "non_nullable":
        target = draw(st.sampled_from(["run_id", "seq", "ts", "pid", "container_id_source", "interval_ms", "dropped"]))
        out = dict(obj)
        out["null_reasons"] = {**(obj.get("null_reasons") or {}), target: draw(st.one_of(ok_reason, st.sampled_from([5, None, []])))}
        code, name = "unknown_field", f"null_reasons.{target}"
    elif sub == "non_null_field":
        out = make_value(obj, nullable, VALID_VALUE[nullable])
        out["null_reasons"] = {**(out.get("null_reasons") or {}), nullable: draw(ok_reason)}
        code, name = "invalid_format", f"null_reasons.{nullable}"
    elif sub == "unknown_key":
        key = draw(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=20).filter(lambda k: k not in NULLABLE_F))
        out = dict(obj)
        out["null_reasons"] = {**(obj.get("null_reasons") or {}), key: draw(st.one_of(ok_reason, st.sampled_from([5, None, []])))}
        code, name = "unknown_field", f"null_reasons.{key}"
    else:  # not an object; every nullable field is made non-null first so that nothing else is wrong
        out = dict(obj)
        for n in NULLABLE_F:
            out = make_value(out, n, VALID_VALUE[n])
        out["null_reasons"] = draw(st.sampled_from([[], "x", 5, 1.5, True, False, None, [{}], ["a"]]))
        code, name = "wrong_type", "null_reasons"
    return schema_case(20, out, s, obj, code, name, {"null_reasons"}, sub=sub)


BAD_TS = ["", "not-a-time", "2025-01-01", "2025-13-01T00:00:00.000Z", "2025-01-01T12:00:06.512", "2025-01-01T12:00:06Z", "2025-02-30T00:00:00.000Z", "1/2/2025"]
BAD_CONTAINER = ["", "xyz", "ABCDEF012345", "0" * 11, "0" * 13, "0" * 63, "0" * 65, "g" * 12]
BAD_SOURCE = ["", "MOUNTINFO", "docker", "none "]


def b21(draw: Callable[[Any], Any], obj: dict[str, Any], s: Style) -> Mutation:
    name = draw(st.sampled_from(["ts", "container_id", "container_id_source"]))
    pool, code = {"ts": (BAD_TS, "invalid_format"), "container_id": (BAD_CONTAINER, "invalid_format"), "container_id_source": (BAD_SOURCE, "out_of_range")}[name]
    return schema_case(21, make_value(obj, name, draw(st.sampled_from(pool))), s, obj, code, name, {name})


BUILDERS: dict[int, Callable[[Callable[[Any], Any], dict[str, Any], Style], Mutation]] = {
    1: b1, 2: b2, 3: b3, 4: b4, 5: b5, 6: b6, 7: b7, 8: b8, 9: b9, 10: b10, 11: b11, 12: b12, 13: b13, 14: b14, 15: b15, 16: b16,
    17: b17, 18: b18, 19: b19, 20: b20, 21: b21,
}
EXTRA_PAD = st.one_of(st.just(0), st.just(0), st.just(0), st.integers(900, 2500))


@st.composite
def mutations(draw: Any, categories: tuple[int, ...] = ALL_CATEGORIES) -> Mutation:
    case = draw(base_cases())
    category = draw(st.sampled_from(categories))
    indent, separators = case["style"]
    style = Style(case["ensure_ascii"], indent, separators, case["pad"], case["pad"] + " " * draw(EXTRA_PAD))
    m = BUILDERS[category](draw, case["sample"], style)
    m.durability = case["durability"]
    return m


# --- harness ----------------------------------------------------------------------------------------------------


class Harness:
    def __init__(self, root: Path, durability: Durability = Durability.CRASH, max_body: int = MAX_BODY) -> None:
        self.now = EARLIER
        self.rec = Rec()
        self.probe = FakeProbe()
        config = CollectorConfig(durability=durability, max_body_bytes=max_body, watchdog_tick_ms=50, evidence_dir=root / "evidence")
        self.directory = config.evidence_dir
        self.service: IngestService = create_ingest_service(
            config, fsync=self.rec.fsync, fsync_dir=self.rec.fsync_dir, now=lambda: self.now, storage_probe=self.probe
        )
        self.service.registry.start()

    def files(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(self.directory.glob("*.jsonl"))}

    def state(self, run_id: str) -> tuple[Any, ...]:
        s = self.service.registry.get(run_id)
        return (s.highest_seq, s.seen.ranges(), s.next_gap_number, s.last_sample_recv_time, s.interval_ms, s.terminated, s.silence_open, s.dirty)

    def put(self, run_id: str, seq: int, **over: Any) -> Any:
        return self.service.ingest(json.dumps(base_sample(run_id=run_id, seq=seq, **over)).encode())

    def preload(self, run_a: str, run_b: str) -> None:
        """Valid history for two runs (with a gap in the first) and one earlier rejection in each rejection file."""
        assert self.put(run_a, 0).http_status == 200 and self.put(run_a, 2).http_status == 200
        assert self.put(run_b, 0).http_status == 200 and self.put(run_b, 1).http_status == 200
        assert self.put(run_a, 3, ts="bad").http_status == 422  # a rejection inside run A's file
        assert self.service.ingest(b"{").http_status == 422  # a rejection in `_rejections.jsonl`
        self.now = NOW


def other_run(run_id: str) -> str:
    return "other-run-b" if run_id.lower() != "other-run-b" else "another-run-c"


def expected_line(m: Mutation, status: int, reason: str, recv: str = NOW) -> bytes:
    raw = m.raw
    record = {
        "record_type": "rejection", "schema_version": 1, "run_id": m.route, "recv_time": recv, "http_status": status,
        "reason_code": m.code, "field": m.field, "reason": reason, "body_len": len(raw),
        "body_preview_b64": base64.b64encode(raw[:PREVIEW_CAP]).decode("ascii"),
    }
    return line_of(record)


def check_rejected(m: Mutation, h: Harness, status: int = 422) -> None:
    """Ingest `m.raw` after a preload, then compare everything with the oracle."""
    run_a = m.base["run_id"]
    run_b = other_run(run_a)
    h.preload(run_a, run_b)
    before, states = h.files(), {r: h.state(r) for r in (run_a, run_b)}
    probe_before = h.probe.invalidations
    result = h.service.ingest(m.raw)
    after = h.files()
    # the result
    assert (result.http_status, result.kind, result.rejection_recorded, result.run_id) == (status, "rejected", True, m.route)
    assert (result.body["reason_code"], result.body["field"]) == (m.code, m.field)
    assert "rejection_recorded" not in result.body and result.body["error"] == ("body_too_large" if m.code == "body_too_large" else "validation_failed")
    assert result.seq is None and result.durability is None and result.gap_ids == () and result.recv_time == NOW
    # the files: nothing new, nothing earlier changed, exactly one rejection line in the expected file
    assert set(after) == set(before), "a file appeared or disappeared"
    target = f"{m.route}.jsonl" if m.route is not None else "_rejections.jsonl"
    for name, data in before.items():
        assert after[name].startswith(data), f"{name}: earlier bytes changed"
        if name != target:
            assert after[name] == data, f"{name} changed but is not the target"
    appended = after[target][len(before[target]):]
    assert appended.endswith(b"\n") and appended.count(b"\n") == 1 and b"\r" not in appended
    assert appended == expected_line(m, status, result.body["reason"]), "the rejection line differs from the oracle's"
    record = json.loads(appended)
    assert validate_record(record) == [] and record["record_type"] == "rejection" and result.body["reason"] != ""
    assert record["recv_time"] == NOW and parse_utc_ms(record["recv_time"]) is not None
    assert record["body_len"] == len(m.raw) and base64.b64decode(record["body_preview_b64"]) == m.raw[:PREVIEW_CAP]
    assert record["body_preview_b64"] == base64.b64encode(m.raw[:PREVIEW_CAP]).decode("ascii")
    assert len(base64.b64decode(record["body_preview_b64"])) == min(len(m.raw), PREVIEW_CAP)
    for name in after:
        assert read_evidence(h.directory / name).errors == [], name
    # sequence state, gaps, probe
    assert {r: h.state(r) for r in (run_a, run_b)} == states, "sequence state moved"
    assert h.probe.invalidations == probe_before
    for r in (run_a, run_b):
        assert not h.service.registry.get(r).dirty


def run_and_check(m: Mutation, *, cross_check: bool = True) -> None:
    check_structure(m)
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp), m.durability)
        try:
            check_rejected(m, h)
        finally:
            h.service.registry.close()
    if cross_check and m.category not in (1, 2, 3, 4, 19):  # only after the oracle: the real validator agrees
        issues = validate_sample(json.loads(m.raw.decode("utf-8")))
        assert issues and (issues[0].reason_code, issues[0].field) == (m.code, m.field)


SETTINGS = settings(
    deadline=None, derandomize=True, database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.large_base_example, HealthCheck.filter_too_much],
)


@settings(parent=SETTINGS, max_examples=300)
@given(mutations())
def test_property_4_invalid_input_is_rejected_and_never_stored(m: Mutation) -> None:
    event(f"category={m.category:02d} {CATEGORY_NAMES[m.category]}")
    event("body over 1024 bytes" if len(m.raw) > PREVIEW_CAP else "body up to 1024 bytes")
    run_and_check(m)


@pytest.mark.parametrize("category", ALL_CATEGORIES, ids=[f"{c:02d}-{CATEGORY_NAMES[c]}" for c in ALL_CATEGORIES])
@settings(parent=SETTINGS, max_examples=20)
@given(data=st.data())
def test_each_category_is_rejected_with_its_expected_result(category: int, data: st.DataObject) -> None:
    m = data.draw(mutations((category,)))
    assert m.category == category
    run_and_check(m)


# --- explicit examples, one per category, with the reason code each must produce --------------------------------------

RID = "run-p4"


def compact(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"))


def explicit_cases() -> list[Mutation]:
    base = base_sample(run_id=RID, seq=7, rss_bytes=None, null_reasons={"rss_bytes": "not measured"})
    full = base_sample(run_id=RID, seq=7)
    ok = compact(full)

    def schema(cat: int, obj: dict[str, Any], code: str, name: str, route: str | None = RID, touched: tuple[str, ...] = (), **info: Any) -> Mutation:
        return Mutation(cat, compact(obj).encode(), code, name, route, full, info={"touched": set(touched), **info})

    def minus(name: str) -> dict[str, Any]:
        return {k: v for k, v in full.items() if k != name}

    def with_(**over: Any) -> dict[str, Any]:
        return {**full, **over}

    return [
        Mutation(1, ok.encode() + b"\xff", "invalid_json", "", None, full),
        Mutation(2, b"not json", "invalid_json", "", None, full),
        Mutation(3, ok.encode()[:-9], "invalid_json", "", None, full, info={"compact": ok}),
        Mutation(4, ok[:-1].encode() + b',"seq":8}', "invalid_json", "", None, full),
        schema(5, minus("pid"), "missing_field", "pid", touched=("pid",)),
        schema(6, with_(seq="7"), "wrong_type", "seq", touched=("seq",)),
        schema(7, minus("run_id"), "invalid_run_id", "run_id", None, ("run_id",)),
        schema(8, with_(run_id=""), "invalid_run_id", "run_id", None, ("run_id",)),
        schema(9, with_(run_id="a" * 65), "invalid_run_id", "run_id", None, ("run_id",)),
        schema(10, with_(run_id="bad id!"), "invalid_run_id", "run_id", None, ("run_id",)),
        schema(11, minus("rss_bytes"), "missing_field", "rss_bytes", touched=("rss_bytes",)),
        schema(12, with_(rss_bytes="1000"), "wrong_type", "rss_bytes", touched=("rss_bytes",)),
        schema(13, with_(rss_bytes=None), "null_without_reason", "rss_bytes", touched=("rss_bytes",)),
        schema(14, minus("loop_delay_ms"), "missing_field", "loop_delay_ms", touched=("loop_delay_ms",)),
        schema(15, with_(loop_delay_ms="0.5"), "wrong_type", "loop_delay_ms", touched=("loop_delay_ms",)),
        schema(16, with_(loop_delay_ms=None), "null_without_reason", "loop_delay_ms", touched=("loop_delay_ms",)),
        schema(17, with_(interval_ms=99), "out_of_range", "interval_ms", touched=("interval_ms",)),
        schema(18, {**full, "zz": 1, "aa": 2}, "unknown_field", "aa", touched=("zz", "aa"), unknown=["zz", "aa"]),
        Mutation(19, ok.replace('"seq":7', '"seq":NaN').encode(), "invalid_json", "", None, full, info={"field": "seq"}),
        schema(20, {**base, "null_reasons": {"rss_bytes": ""}}, "out_of_range", "null_reasons.rss_bytes", touched=("null_reasons",), sub="empty"),
        schema(21, with_(ts="2025-01-01"), "invalid_format", "ts", touched=("ts",)),
    ]


@pytest.mark.parametrize("m", explicit_cases(), ids=[f"{c.category:02d}-{CATEGORY_NAMES[c.category]}" for c in explicit_cases()])
def test_explicit_example_per_category(m: Mutation) -> None:
    run_and_check(m)


def test_explicit_examples_cover_all_categories_with_a_distinct_reason_for_each() -> None:
    cases_ = explicit_cases()
    assert [c.category for c in cases_] == list(ALL_CATEGORIES)
    codes = {c.code for c in cases_}
    assert codes == {"invalid_json", "missing_field", "wrong_type", "invalid_run_id", "null_without_reason", "out_of_range", "unknown_field", "invalid_format"}


def test_large_rejected_bodies_keep_a_capped_preview_and_the_true_length() -> None:
    base = base_sample(run_id=RID)
    big_unknown = compact({**base, "unknown": "x" * 3000}).encode()
    big_garbage = b" " * 5000 + b"{"
    for m in (
        Mutation(18, big_unknown, "unknown_field", "unknown", RID, base),
        Mutation(2, big_garbage, "invalid_json", "", None, base),
    ):
        assert len(m.raw) > PREVIEW_CAP
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            try:
                check_rejected(m, h)
            finally:
                h.service.registry.close()


# --- the structural check itself must notice a mutation that is not in its category ------------------------------------


def test_the_structural_check_rejects_mutations_that_are_not_what_they_claim() -> None:
    good = explicit_cases()
    for m in good:
        check_structure(m)
    full = good[0].base

    def twice(raw: str) -> Mutation:
        return Mutation(5, raw.encode(), "missing_field", "pid", RID, full, info={"touched": {"pid"}})

    base_text = compact(full)
    broken = [
        Mutation(1, base_text.encode(), "invalid_json", "", None, full),  # valid UTF-8
        Mutation(2, base_text.encode(), "invalid_json", "", None, full),  # parses
        Mutation(3, base_text.encode()[:-1] + b"} ", "invalid_json", "", None, full, info={"compact": base_text}),  # not a truncation
        Mutation(4, base_text.encode(), "invalid_json", "", None, full),  # no duplicate
        twice(compact({k: v for k, v in full.items() if k not in ("pid", "ts")})),  # two fields missing
        Mutation(6, compact({**full, "seq": -1}).encode(), "wrong_type", "seq", RID, full, info={"touched": {"seq"}}),  # out of range, not a type error
        Mutation(17, compact({**full, "seq": "x"}).encode(), "out_of_range", "seq", RID, full, info={"touched": {"seq"}}),
        Mutation(9, compact({**full, "run_id": "bad id"}).encode(), "invalid_run_id", "run_id", None, full, info={"touched": {"run_id"}}),  # short, not long
        Mutation(5, compact({**{k: v for k, v in full.items() if k != "pid"}, "seq": "x"}).encode(), "missing_field", "pid", RID, full, info={"touched": {"pid", "seq"}}),
        Mutation(19, base_text.encode(), "invalid_json", "", None, full, info={"field": "seq"}),
    ]
    for m in broken:
        with pytest.raises(AssertionError):
            check_structure(m)


# --- failed rejection write -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["write", "partial", "flush", "fsync"])
@pytest.mark.parametrize("route", ["run", "rejections"])
def test_a_failed_rejection_write_keeps_the_422_marks_the_file_dirty_and_is_recovered_before_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, mode: str
) -> None:
    h = Harness(tmp_path)
    faults = Faults(monkeypatch)
    run_a, run_b = "run-a", "run-b"
    h.preload(run_a, run_b)
    name = f"{run_a}.jsonl" if route == "run" else "_rejections.jsonl"
    raw = json.dumps(base_sample(run_id=run_a, seq=9, ts="bad")).encode() if route == "run" else b"{"
    code, field_ = ("invalid_format", "ts") if route == "run" else ("invalid_json", "")
    before, states, probe_before = h.files(), {r: h.state(r) for r in (run_a, run_b)}, h.probe.invalidations
    if mode == "fsync":
        h.rec.fail_file = True
    else:
        faults.arm(mode, name)
    result = h.service.ingest(raw)
    # the original 422 stays, the Collector does not claim a recording, the probe is told, the file is gated
    assert (result.http_status, result.kind, result.rejection_recorded) == (422, "rejected", False)
    assert result.body["reason_code"] == code and result.body["field"] == field_ and result.body["rejection_recorded"] is False
    assert h.probe.invalidations == probe_before + 1
    if route == "run":
        assert h.service.registry.get(run_a).dirty
        assert {r: h.state(r)[:-1] for r in (run_a, run_b)} == {r: s[:-1] for r, s in states.items()}  # sequence state untouched
    else:
        assert h.service.registry.rejections_dirty
    middle = h.files()
    assert all(middle[n].startswith(d) for n, d in before.items()), "earlier bytes changed"
    assert not any(b'"record_type":"sample"' in middle[n][len(d):] for n, d in before.items()), "a sample was written"
    # recovery: the next request to the same file succeeds and the file reads cleanly
    h.rec.fail_file = False
    faults.disarm()
    if route == "run":
        again = h.put(run_a, 4)
        assert again.http_status == 200 and again.body["status"] == "stored"
        assert not h.service.registry.get(run_a).dirty
    else:
        again = h.service.ingest(raw)
        assert again.http_status == 422 and again.rejection_recorded is True and "rejection_recorded" not in again.body
        assert not h.service.registry.rejections_dirty
    final = h.files()
    assert all(final[n].startswith(d) for n, d in middle.items()), "recovery changed earlier bytes"
    read = read_evidence(h.directory / name)
    if mode == "partial":
        # exactly one Malformed_Line: the cut rejection line, isolated by the repair that ran before the next append
        fragment = middle[name][len(before[name]):]
        assert fragment and not fragment.endswith(b"\n")
        (error,) = read.errors
        assert (error.file, error.line_no, error.kind, error.has_repair_record) == (name, before[name].count(b"\n") + 1, "isolated_by_repair", True)
        assert error.byte_length == len(fragment) + 1  # the fragment plus the D30 NUL marker
        assert final[name][len(before[name]):].startswith(fragment + b"\x00\n"), "the original fragment is not an unchanged prefix"
    else:
        assert read.errors == [], read.errors
    assert all(validate_record(r) == [] for r in read.records)
    other = f"{run_b}.jsonl"
    assert final[other] == before[other], "another run's file changed"
    h.service.registry.close()


# --- request-size boundary (413 is a separate contract) ---------------------------------------------------------------


class ParseSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        original = service_module._parse_body

        def spy(raw: bytes) -> Any:
            self.calls += 1
            return original(raw)

        monkeypatch.setattr(service_module, "_parse_body", spy)


def padded(text: str, size: int) -> bytes:
    raw = text.encode()
    assert len(raw) <= size
    return raw + b" " * (size - len(raw))


def test_the_size_limit_is_exact_and_oversized_bodies_are_never_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    limit = 3000
    h = Harness(tmp_path, max_body=limit)
    spy = ParseSpy(monkeypatch)
    valid = compact(base_sample(run_id="r1", seq=0))
    invalid = compact(base_sample(run_id="r1", seq=1, ts="bad"))
    # exactly at the limit: processed normally
    ok = h.service.ingest(padded(valid, limit))
    assert (ok.http_status, ok.kind, spy.calls) == (200, "stored", 1)
    bad = h.service.ingest(padded(invalid, limit))
    assert (bad.http_status, bad.body["reason_code"], bad.run_id, spy.calls) == (422, "invalid_format", "r1", 2)
    malformed = h.service.ingest(padded("{", limit))
    assert (malformed.http_status, malformed.body["reason_code"], spy.calls) == (422, "invalid_json", 3)
    files_before = h.files()
    # one byte over: 413 for a valid Sample, for malformed JSON and for invalid UTF-8, and nothing is parsed
    for raw in (padded(valid.replace('"seq":0', '"seq":2'), limit + 1), padded("{", limit + 1), b"\xff" * (limit + 1)):
        r = h.service.ingest(raw)
        assert (r.http_status, r.kind, r.run_id, r.rejection_recorded) == (413, "rejected", None, True)
        assert r.body["error"] == "body_too_large" and r.body["reason_code"] == "body_too_large"
    assert spy.calls == 3, "an oversized body was parsed"
    after = h.files()
    assert set(after) == set(files_before) and all(after[n].startswith(d) for n, d in files_before.items())
    assert after["r1.jsonl"] == files_before["r1.jsonl"], "an oversized Sample reached the run file"
    last = [json.loads(line) for line in after["_rejections.jsonl"].splitlines()][-3:]
    for record, size in zip(last, (limit + 1,) * 3):
        assert validate_record(record) == [] and record["http_status"] == 413 and record["reason_code"] == "body_too_large" and record["run_id"] is None
        assert record["body_len"] == size and len(base64.b64decode(record["body_preview_b64"])) == PREVIEW_CAP
    assert h.service.registry.get("r1").highest_seq == 0
    h.service.registry.close()


def test_the_default_limit_is_unchanged() -> None:
    from coroner.collector.config import DEFAULT_MAX_BODY_BYTES

    assert DEFAULT_MAX_BODY_BYTES == 65536 == MAX_BODY
