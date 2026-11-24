"""Stat-signature cache tests for the Viewer listing (task 2.17).

The listing cache is keyed by the stat signature `(size, mtime_ns, dev, ino)` and stores a result only when
the signature did not change during the load and no artifact was modified within 2 seconds of the injected
clock ("racily clean"). Every test uses synthetic runs under `tmp_path`, sets file modification times
explicitly with `os.utime`, and injects the clock; nothing reads the real `runs/` directory.

Design: "Listing cost", items 2 to 4. Requirements: 3.18, 16.11 (stated limitation), 16.10.
"""
from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from coroner.runner.manifest import Failure, deserialize_manifest, serialize_manifest
from coroner.viewer.files import DEFAULT_IO, open_runs_root
from coroner.viewer.loader import RunCache, load_listing
from tests.unit.viewer.builder import (
    EVIDENCE_NAME,
    INSPECT_NAME,
    MANIFEST_NAME,
    InspectSpec,
    RunsBuilder,
    build_inspect_document,
    sample_specs,
)

T0 = 1_700_000_000 * 10**9  # an old, fixed modification time (nanoseconds)
SECOND = 10**9
RUN = "b-cache-00000001"
OTHER = "b-cache-00000002"


def _clock_far() -> int:
    """A clock one hour after T0: every artifact aged to T0 is old enough to be cached."""
    return T0 + 3600 * SECOND


class SpyCache(RunCache):
    """A RunCache that records every `put`."""

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[tuple[int, int, int, int]] = []

    def put(self, sig: tuple[int, int, int, int], facts: Any) -> None:
        self.puts.append(sig)
        super().put(sig, facts)


def _set_mtime(path: Path, ns: int) -> None:
    os.utime(path, ns=(ns, ns))


def _age(run_dir: Path) -> None:
    for name in (MANIFEST_NAME, EVIDENCE_NAME, INSPECT_NAME):
        if (run_dir / name).exists():
            _set_mtime(run_dir / name, T0)


def _facts(
    runs: Path,
    cache: RunCache | None,
    clock: Callable[[], int] = _clock_far,
    io: Any = DEFAULT_IO,
) -> list[dict[str, Any]]:
    listing = load_listing(open_runs_root(runs), io, clock, cache)
    return [dataclasses.asdict(f) for f in listing["facts"]]


class Env:
    """Two aged runs under tmp_path (the second one is never changed)."""

    def __init__(self, tmp_path: Path, **first_kwargs: Any) -> None:
        self.runs = tmp_path / "runs"
        self.builder = RunsBuilder(self.runs)
        self.builder.add_run("B", run_id=RUN, samples=sample_specs(3), **first_kwargs)
        self.builder.add_run("B", run_id=OTHER, samples=sample_specs(3))
        self.dir = self.runs / RUN
        for run_id in (RUN, OTHER):
            _age(self.runs / run_id)

    def manifest_bytes(self) -> bytes:
        return (self.dir / MANIFEST_NAME).read_bytes()

    def write_manifest(self, *, mtime: int = T0, **changes: Any) -> None:
        manifest = dataclasses.replace(deserialize_manifest(self.manifest_bytes()), **changes)
        (self.dir / MANIFEST_NAME).write_bytes(serialize_manifest(manifest))
        _set_mtime(self.dir / MANIFEST_NAME, mtime)

    def replace_file(self, name: str, data: bytes, *, mtime: int = T0) -> None:
        """Write `data` to a new file and move it over `name` (a new file identity)."""
        temp = self.dir / (name + ".new")
        temp.write_bytes(data)
        _set_mtime(temp, mtime)
        os.replace(temp, self.dir / name)


FAILED = {"outcome": "failed", "failure": Failure("synthetic_failure", "a failure message that is long")}


def _sample_lines(env: Env) -> list[bytes]:
    return (env.dir / EVIDENCE_NAME).read_bytes().splitlines(keepends=True)


def _more_samples(env: Env) -> bytes:
    lines = _sample_lines(env)
    record = json.loads(lines[-1])
    record["sample"]["seq"] += 1
    return b"".join(lines) + json.dumps(record, separators=(",", ":")).encode("ascii") + b"\n"


# ---------------------------------------------------------------- changes are visible compared with an empty cache


def _created(env: Env) -> None:
    env.builder.add_run("A", run_id="a-cache-created-001", container_state=None)


def _manifest_grown(env: Env) -> None:
    before = len(env.manifest_bytes())
    env.write_manifest(mtime=T0 + 5 * SECOND, **FAILED)  # larger file, verdict changes
    assert len(env.manifest_bytes()) > before


def _manifest_rewritten_mtime(env: Env) -> None:
    # Same size and same file, only the modification time moves.
    manifest = dataclasses.replace(deserialize_manifest(env.manifest_bytes()), experiment="A")
    data = serialize_manifest(manifest)
    assert len(data) == len(env.manifest_bytes())
    (env.dir / MANIFEST_NAME).write_bytes(data)
    _set_mtime(env.dir / MANIFEST_NAME, T0 + 10 * SECOND)


def _manifest_rewritten_identity(env: Env) -> None:
    # Same size, same modification time, a different file (new inode).
    manifest = dataclasses.replace(deserialize_manifest(env.manifest_bytes()), experiment="A")
    data = serialize_manifest(manifest)
    assert len(data) == len(env.manifest_bytes())
    before = os.stat(env.dir / MANIFEST_NAME)
    env.replace_file(MANIFEST_NAME, data, mtime=T0)
    after = os.stat(env.dir / MANIFEST_NAME)
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)


def _manifest_deleted(env: Env) -> None:
    (env.dir / MANIFEST_NAME).unlink()


def _manifest_replaced(env: Env) -> None:
    manifest = dataclasses.replace(deserialize_manifest(env.manifest_bytes()), **FAILED)
    env.replace_file(MANIFEST_NAME, serialize_manifest(manifest), mtime=T0 + 5 * SECOND)


def _evidence_grown(env: Env) -> None:
    (env.dir / EVIDENCE_NAME).write_bytes(_more_samples(env))
    _set_mtime(env.dir / EVIDENCE_NAME, T0 + 5 * SECOND)


def _evidence_shrunk(env: Env) -> None:
    (env.dir / EVIDENCE_NAME).write_bytes(b"".join(_sample_lines(env)[:-1]))
    _set_mtime(env.dir / EVIDENCE_NAME, T0 + 5 * SECOND)


def _evidence_deleted(env: Env) -> None:
    (env.dir / EVIDENCE_NAME).unlink()


def _evidence_replaced(env: Env) -> None:
    env.replace_file(EVIDENCE_NAME, _more_samples(env), mtime=T0 + 5 * SECOND)


def _inspect_rewritten(env: Env) -> None:
    doc = build_inspect_document(InspectSpec(exit_code=1, oom_killed=True), RUN)
    (env.dir / INSPECT_NAME).write_bytes(doc)
    _set_mtime(env.dir / INSPECT_NAME, T0 + 5 * SECOND)


def _inspect_deleted(env: Env) -> None:
    (env.dir / INSPECT_NAME).unlink()


CHANGES = {
    "created": _created,
    "manifest_rewritten_size_grown": _manifest_grown,
    "manifest_rewritten_mtime": _manifest_rewritten_mtime,
    "manifest_rewritten_identity": _manifest_rewritten_identity,
    "manifest_deleted": _manifest_deleted,
    "manifest_replaced": _manifest_replaced,
    "evidence_grown": _evidence_grown,
    "evidence_shrunk": _evidence_shrunk,
    "evidence_deleted": _evidence_deleted,
    "evidence_replaced": _evidence_replaced,
    "inspect_rewritten": _inspect_rewritten,
    "inspect_deleted": _inspect_deleted,
}


@pytest.mark.parametrize("change", list(CHANGES))
def test_changed_artifact_gives_the_same_list_result_as_an_empty_cache(tmp_path: Path, change: str) -> None:
    env = Env(tmp_path)
    cache = SpyCache()
    warm = _facts(env.runs, cache)
    assert cache.puts, "the first listing must store its results (artifacts are old enough)"

    CHANGES[change](env)

    fresh = _facts(env.runs, RunCache())
    cached = _facts(env.runs, cache)
    assert fresh != warm, "the change must be visible to an uncached listing"
    assert cached == fresh


def test_manifest_shrunk_changes_the_list_result(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.write_manifest(**FAILED)
    _age(env.dir)
    cache = SpyCache()
    warm = _facts(env.runs, cache)
    assert next(f for f in warm if f["run_id"] == RUN)["verdict"] == "FAIL"

    env.write_manifest(outcome="passed", failure=None, mtime=T0 + 5 * SECOND)

    fresh = _facts(env.runs, RunCache())
    assert fresh != warm
    assert _facts(env.runs, cache) == fresh


def test_an_unchanged_artifact_is_served_from_the_cache(tmp_path: Path) -> None:
    env = Env(tmp_path)
    cache = SpyCache()
    _facts(env.runs, cache)

    class CountingIO:
        opens: list[str] = []

        def __getattr__(self, name: str) -> Any:
            return getattr(DEFAULT_IO, name)

        def open(self, path: str, flags: int) -> int:
            self.opens.append(os.path.basename(path))
            return DEFAULT_IO.open(path, flags)

    io = CountingIO()
    again = _facts(env.runs, cache, io=io)

    assert again == _facts(env.runs, RunCache())
    assert EVIDENCE_NAME not in io.opens and INSPECT_NAME not in io.opens


# ---------------------------------------------------------------- results that must not be stored


def test_result_is_not_stored_when_mtime_is_within_two_seconds_of_the_clock(tmp_path: Path) -> None:
    env = Env(tmp_path)
    for offset, stored in ((int(1.9 * SECOND), False), (0, False), (-5 * SECOND, False), (3 * SECOND, True)):
        cache = SpyCache()
        _facts(env.runs, cache, clock=lambda offset=offset: T0 + offset)
        assert bool(cache.puts) is stored, offset


def test_result_is_not_stored_when_another_artifact_is_recent(tmp_path: Path) -> None:
    env = Env(tmp_path)
    for name in (EVIDENCE_NAME, INSPECT_NAME):
        _set_mtime(env.dir / name, _clock_far() - SECOND)  # the manifest stays old
    cache = SpyCache()

    _facts(env.runs, cache)

    # OTHER is stored (all of its artifacts are old); RUN has a recent evidence and inspect file.
    other_ino = os.stat(env.runs / OTHER / MANIFEST_NAME).st_ino
    assert [sig[3] for sig in cache.puts] == [other_ino], cache.puts


def test_result_is_not_stored_when_the_signature_changes_during_the_load(tmp_path: Path) -> None:
    env = Env(tmp_path)
    original_manifest = env.manifest_bytes()

    class ChangingIO:
        """Rewrites the manifest of RUN (new size) when its evidence file is opened, i.e. during the load."""

        def __init__(self) -> None:
            self.done = False

        def __getattr__(self, name: str) -> Any:
            return getattr(DEFAULT_IO, name)

        def open(self, path: str, flags: int) -> int:
            if not self.done and os.path.basename(path) == EVIDENCE_NAME and RUN in path:
                self.done = True
                env.write_manifest(**FAILED)
            return DEFAULT_IO.open(path, flags)

    cache = SpyCache()
    io = ChangingIO()

    _facts(env.runs, cache, io=io)

    assert io.done and env.manifest_bytes() != original_manifest
    # Only OTHER (unchanged during its load) may be stored; nothing is stored for RUN, which changed.
    other_ino = os.stat(env.runs / OTHER / MANIFEST_NAME).st_ino
    assert [sig[3] for sig in cache.puts] == [other_ino], "RUN changed during its load and must not be stored"


# ---------------------------------------------------------------- the documented limitation


def test_same_signature_rewrite_is_served_from_the_cache(tmp_path: Path) -> None:
    """DOCUMENTED LIMITATION, not a guarantee (design: Listing cost item 4; loader.py docstring, R-F).

    The cache key is `(size, mtime_ns, dev, ino)`. An artifact that is rewritten in place with the same
    size while keeping its modification time and file identity has the same signature, so the cached list
    result is served and the new content is NOT seen until the Viewer restarts. This test pins that
    behaviour as an example. It does not claim the behaviour is desirable and it must never be read as
    "the cache is always fresh".
    """
    env = Env(tmp_path)
    cache = SpyCache()
    warm = _facts(env.runs, cache)

    manifest = dataclasses.replace(deserialize_manifest(env.manifest_bytes()), experiment="A")
    data = serialize_manifest(manifest)
    assert len(data) == len(env.manifest_bytes())
    before = os.stat(env.dir / MANIFEST_NAME)
    with open(env.dir / MANIFEST_NAME, "r+b") as handle:  # in place: same file identity
        handle.write(data)
    _set_mtime(env.dir / MANIFEST_NAME, T0)
    after = os.stat(env.dir / MANIFEST_NAME)
    assert (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino) == (
        before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino,
    )

    served = _facts(env.runs, cache)
    fresh = _facts(env.runs, RunCache())

    assert served == warm
    assert fresh != warm
