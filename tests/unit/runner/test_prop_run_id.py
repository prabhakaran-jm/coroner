"""Property 24: Run identifiers are unique and valid (task 6.10).

*For any* set of existing Run_Identifiers and any experiment, the generated Run_Identifier is not
in the set and matches the permitted pattern.

**Validates: Requirements 9.1, 10.1, 11.1, 12.1**

The oracle is a separately written regular expression for the documented shape
`<experiment>-<yyyymmddThhmmssZ>-<8 lowercase hex>` plus the imported `RUN_ID_RE`; it shares no code
with `generate_run_id`. Clock and randomness are injected, so every case is deterministic.
No test touches the repository `runs/` directory; directories live under temporary paths.
"""
from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coroner.evidence.schema import RUN_ID_RE
from coroner.runner.manifest import (
    MAX_RUN_ID_ATTEMPTS,
    RunIdError,
    RunIdExhaustedError,
    generate_run_id,
)

SHAPE = re.compile(r"[a-z0-9]{1,16}-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")

experiments = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=16)
hex8 = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)
instants = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2099, 12, 31), timezones=st.just(timezone.utc)
)


def check_valid(run_id: str, exp: str, existing: set[str]) -> None:
    """Independent oracle for Property 24."""
    assert RUN_ID_RE.fullmatch(run_id) is not None
    assert SHAPE.fullmatch(run_id) is not None
    assert len(run_id) <= 64
    assert run_id.startswith(exp + "-")
    assert run_id not in existing
    assert run_id.lower() not in {e.lower() for e in existing}


def seq_hex(values: list[str]):
    it = iter(values)
    return lambda: next(it)


@settings(max_examples=150, deadline=None)
@given(
    exp=experiments,
    now=instants,
    existing=st.sets(st.text(max_size=70), max_size=20),
    hexes=st.lists(hex8, min_size=1, max_size=5, unique=True),
)
def test_generated_id_valid_and_unique(exp, now, existing, hexes):
    # Adversarial: pre-seed the set with ids the first draws would produce.
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    taken = {f"{exp}-{stamp}-{h}" for h in hexes[:-1]}
    known = set(existing) | taken
    final = f"{exp}-{stamp}-{hexes[-1]}"
    known.discard(final)
    run_id = generate_run_id(exp, known, clock=lambda: now, rand_hex=seq_hex(hexes))
    check_valid(run_id, exp, known)
    assert run_id == final


@settings(max_examples=60, deadline=None)
@given(exp=experiments, now=instants, hexes=st.lists(hex8, min_size=2, max_size=4, unique=True))
def test_existing_run_directories_are_avoided(exp, now, hexes):
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory() as tmp:
        for h in hexes[:-1]:
            (Path(tmp) / f"{exp}-{stamp}-{h}").mkdir()
        run_id = generate_run_id(
            exp, (), runs_dir=tmp, clock=lambda: now, rand_hex=seq_hex(hexes)
        )
        assert run_id == f"{exp}-{stamp}-{hexes[-1]}"
        assert not (Path(tmp) / run_id).exists()


@settings(max_examples=60, deadline=None)
@given(exp=experiments)
def test_real_clock_and_randomness_produce_distinct_valid_ids(exp):
    ids: set[str] = set()
    for _ in range(25):
        run_id = generate_run_id(exp, ids)
        check_valid(run_id, exp, ids)
        ids.add(run_id)
    assert len(ids) == 25


def test_non_utc_clock_is_converted_to_utc():
    tz = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2025, 1, 1, 17, 45, 0, tzinfo=tz)
    rid = generate_run_id("c", clock=lambda: now, rand_hex=lambda: "4be07a11")
    assert rid == "c-20250101T121500Z-4be07a11"


def test_experiment_is_lowercased():
    now = datetime(2025, 1, 1, 12, 15, 0, tzinfo=timezone.utc)
    assert generate_run_id("C", clock=lambda: now, rand_hex=lambda: "00000000").startswith("c-")


def test_case_insensitive_collision_is_retried():
    now = datetime(2025, 1, 1, 12, 15, 0, tzinfo=timezone.utc)
    hexes = seq_hex(["aaaaaaaa", "bbbbbbbb"])
    rid = generate_run_id(
        "a", {"A-20250101T121500Z-AAAAAAAA"}, clock=lambda: now, rand_hex=hexes
    )
    assert rid.endswith("-bbbbbbbb")


def test_collision_every_time_is_bounded():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    calls: list[int] = []

    def rand() -> str:
        calls.append(1)
        return "deadbeef"

    with pytest.raises(RunIdExhaustedError):
        generate_run_id("a", {"a-20250101T000000Z-deadbeef"}, clock=lambda: now, rand_hex=rand)
    assert len(calls) == MAX_RUN_ID_ATTEMPTS
    calls.clear()
    with pytest.raises(RunIdExhaustedError):
        generate_run_id(
            "a", {"a-20250101T000000Z-deadbeef"}, clock=lambda: now, rand_hex=rand, max_attempts=3
        )
    assert len(calls) == 3


@pytest.mark.parametrize(
    "bad", ["", "../x", "a/b", "a b", "A_B", "x" * 17, "é", "a\n", "_rej", "a.b", "a-b"]
)
def test_invalid_experiment_rejected(bad):
    with pytest.raises(RunIdError):
        generate_run_id(bad)


@pytest.mark.parametrize("bad", ["DEADBEEF", "1234567", "123456789", "zzzzzzzz", "", "abcdefg\n"])
def test_bad_randomness_seam_rejected(bad):
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(RunIdError):
        generate_run_id("a", clock=lambda: now, rand_hex=lambda: bad)


def test_naive_clock_rejected():
    with pytest.raises(RunIdError):
        generate_run_id("a", clock=lambda: datetime(2025, 1, 1))


def test_runs_dir_collision_with_plain_file(tmp_path):
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    (tmp_path / "a-20250101T000000Z-11111111").write_bytes(b"")
    rid = generate_run_id(
        "a", runs_dir=tmp_path, clock=lambda: now, rand_hex=seq_hex(["11111111", "22222222"])
    )
    assert rid.endswith("22222222")


# ---------------------------------------------------------------- negative controls (fixtures only)


def naive_generator_ignores_existing(exp, existing, now, hexes):
    return f"{exp}-{now.strftime('%Y%m%dT%H%M%SZ')}-{hexes[0]}"


def naive_generator_uppercase(exp, existing, now, hexes):
    return f"{exp}-{now.strftime('%Y%m%dT%H%M%SZ')}-{hexes[0].upper()}"


def test_negative_control_oracle_detects_ignoring_existing():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    existing = {"a-20250101T000000Z-aaaaaaaa"}
    bad = naive_generator_ignores_existing("a", existing, now, ["aaaaaaaa"])
    with pytest.raises(AssertionError):
        check_valid(bad, "a", existing)


def test_negative_control_oracle_detects_uppercase_hex():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bad = naive_generator_uppercase("a", set(), now, ["abcdef12"])
    with pytest.raises(AssertionError):
        check_valid(bad, "a", set())
