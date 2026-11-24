"""Parity of the Viewer's local Docker_Time_Parser with the M0 ``parse_docker_time``.

This is the only Viewer test that imports ``coroner.runner.experiments.base``.
"""

from __future__ import annotations

import ast
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

import coroner.viewer
from coroner.runner.experiments.base import parse_docker_time as m0_parse
from coroner.viewer import timefmt
from coroner.viewer.timefmt import parse_docker_time as local_parse

FIXED = [
    "2024-05-01T12:34:56.123456789Z",  # nanoseconds
    "2024-05-01T12:34:56.123456Z",  # microseconds
    "2024-05-01T12:34:56.123Z",  # milliseconds
    "2024-05-01T12:34:56Z",  # no fraction
    "2024-05-01T12:34:56.1Z",
    "2024-05-01T12:34:56.1234567890Z",  # ten digits, rejected
    "0001-01-01T00:00:00Z",  # zero date
    "0001-01-01T00:00:00.000000000Z",
    "0002-01-01T00:00:00Z",
    "2024-05-01T12:34:56+00:00",  # offsets
    "2024-05-01T12:34:56.123+02:00",
    "2024-05-01t12:34:56z",  # lower case
    "2024-05-01t12:34:56Z",
    "2024-05-01T12:34:56z",
    "2024-05-01T12:34:56Z\n",  # trailing newline
    " 2024-05-01T12:34:56Z",
    "2024-02-30T12:34:56Z",  # impossible calendar value
    "2024-13-01T12:34:56Z",
    "2024-05-01T24:00:00Z",
    "2024-05-01T12:34:60Z",
    "",
    "not a time",
    "2024-05-01 12:34:56Z",
    "2024-05-01T12:34:56.Z",
    "\u0662024-05-01T12:34:56Z",  # non-ASCII digit
    None,
    123,
    1.5,
    True,
    b"2024-05-01T12:34:56Z",
    ["2024-05-01T12:34:56Z"],
    {},
]


def _outcome(fn, value):
    try:
        return ("ok", fn(value))
    except Exception as exc:  # noqa: BLE001 - parity of exception types is the assertion
        return ("raised", type(exc))


@pytest.mark.parametrize("value", FIXED, ids=[repr(v)[:40] for v in FIXED])
def test_fixed_examples_match_m0(value):
    assert _outcome(local_parse, value) == _outcome(m0_parse, value)


def test_fixed_examples_have_expected_shapes():
    got = local_parse("2024-05-01T12:34:56.123456789Z")
    assert got == datetime(2024, 5, 1, 12, 34, 56, 123456, tzinfo=timezone.utc)
    assert got.tzinfo is not None and got.utcoffset().total_seconds() == 0
    assert local_parse("0001-01-01T00:00:00Z") is None
    assert local_parse("2024-05-01T12:34:56Z\n") is None
    assert local_parse(None) is None


def _generated(rng: random.Random) -> str:
    year = rng.choice([0, 1, 2, 1970, 2024, 9999, rng.randrange(10000)])
    base = (
        f"{year:04d}-{rng.choice([0, 1, 2, 12, 13, rng.randrange(100)]):02d}"
        f"-{rng.choice([0, 1, 28, 29, 30, 31, 32, rng.randrange(100)]):02d}"
        f"T{rng.choice([0, 12, 23, 24, rng.randrange(100)]):02d}"
        f":{rng.choice([0, 59, 60, rng.randrange(100)]):02d}"
        f":{rng.choice([0, 59, 60, rng.randrange(100)]):02d}"
    )
    frac = rng.choice(["", "." + "".join(rng.choice("0123456789") for _ in range(rng.randrange(0, 13)))])
    tail = rng.choice(["Z", "Z", "Z", "z", "+00:00", "-01:00", "", "Z\n", " Z", "ZZ"])
    text = base + frac + tail
    if rng.random() < 0.2:
        text = text.replace("T", rng.choice(["t", " ", "_"]), 1)
    if rng.random() < 0.1:
        pos = rng.randrange(len(text) + 1)
        text = text[:pos] + rng.choice("0aZ:.-\u0663 ") + text[pos:]
    return text


def test_seeded_generated_strings_match_m0():
    rng = random.Random(20240501)
    for _ in range(3000):
        value = _generated(rng)
        assert _outcome(local_parse, value) == _outcome(m0_parse, value), value


def test_local_parser_is_the_exported_function():
    assert timefmt.parse_docker_time is local_parse


def test_no_viewer_module_imports_the_m0_experiments_base():
    root = Path(coroner.viewer.__file__).parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [module] + [f"{module}.{a.name}" for a in node.names]
            if any(n == "coroner.runner.experiments.base" or n.startswith("coroner.runner.experiments.base.") for n in names):
                offenders.append(path.name)
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "coroner.runner.experiments"
                and any(a.name == "base" for a in node.names)
            ):
                offenders.append(path.name)
    assert offenders == []
