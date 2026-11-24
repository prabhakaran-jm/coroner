"""File-access tests for coroner.viewer.files: containment, bounds, listing entries (task 1.9).

Every fixture lives under pytest's `tmp_path` and is built with `RunsBuilder`. Nothing here reads the
real `runs/` directory, starts a subprocess or opens a socket. Link and reparse behaviour is covered two
ways: real symlinks through `RunsBuilder.try_symlink` (skipped with a reason when the account cannot
create them) and a fake `ArtifactIO` whose `lstat` reports `st_file_attributes` with the
`FILE_ATTRIBUTE_REPARSE_POINT` bit, which runs on every platform.

Requirements: 2.2, 2.6, 2.8, 3.1, 3.2, 3.15, 11.2, 11.6, 11.15, 11.22, 11.30, 16.1-16.6, 16.9.
"""
from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from coroner.viewer import files
from coroner.viewer.files import (
    ARTIFACT_LIMIT,
    DEFAULT_IO,
    READ_CHUNK,
    READ_LIMIT,
    ArtifactFault,
    ArtifactName,
    ArtifactStatus,
    RunIdError,
    RunsRootError,
    find_run,
    list_run_entries,
    open_runs_root,
    read_artifact,
)
from tests.unit.viewer.builder import RunsBuilder

REPARSE = files.FILE_ATTRIBUTE_REPARSE_POINT
ALL_NAMES = list(ArtifactName)


# ---------------------------------------------------------------- helpers


@dataclass(frozen=True)
class FakeStat:
    """Duck-typed stat result with exactly the fields files.py reads."""

    st_mode: int
    st_ino: int
    st_dev: int
    st_size: int
    st_mtime_ns: int
    st_file_attributes: int = 0


def fake_stat(real: os.stat_result, **over: int) -> FakeStat:
    fields = {
        "st_mode": real.st_mode,
        "st_ino": real.st_ino,
        "st_dev": real.st_dev,
        "st_size": real.st_size,
        "st_mtime_ns": real.st_mtime_ns,
        "st_file_attributes": getattr(real, "st_file_attributes", 0),
    }
    fields.update(over)
    return FakeStat(**fields)


class FakeIO:
    """ArtifactIO that delegates to DEFAULT_IO unless a hook is given.

    A hook is called as ``hook(base_method, *args)`` so it can wrap the real call.
    """

    def __init__(self, **hooks):
        self.hooks = hooks
        self.calls: list[tuple] = []

    def _go(self, op, *args):
        self.calls.append((op, *args))
        base = getattr(DEFAULT_IO, op)
        hook = self.hooks.get(op)
        return hook(base, *args) if hook is not None else base(*args)

    def scandir(self, path):
        return self._go("scandir", path)

    def lstat(self, path):
        return self._go("lstat", path)

    def open(self, path, flags):
        return self._go("open", path, flags)

    def fstat(self, fd):
        return self._go("fstat", fd)

    def read(self, fd, n):
        return self._go("read", fd, n)

    def close(self, fd):
        return self._go("close", fd)

    def realpath(self, path, strict=True):
        return self._go("realpath", path, strict)


class Env:
    """One synthetic Runs_Directory with one listed run."""

    def __init__(self, tmp_path: Path, experiment: str = "B") -> None:
        self.tmp_path = tmp_path
        self.runs = tmp_path / "runs"
        self.builder = RunsBuilder(self.runs)
        self.run_id = self.builder.add_run(experiment)
        self.root = open_runs_root(self.runs)
        entries = list_run_entries(self.root)
        assert [e.run_id for e in entries] == [self.run_id]
        self.run = entries[0]
        self.dir_path = os.path.join(self.root.real, self.run_id)

    def artifact_path(self, name: ArtifactName) -> str:
        return os.path.join(self.dir_path, name.value)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def assert_no_path(result, env: Env) -> None:
    """No absolute path (or the unique marker used in injected OSError text) in any text form."""
    text = repr(result) + str(result) + (getattr(result, "reason", None) or "")
    for secret in (env.root.real, str(env.tmp_path), str(env.runs), env.tmp_path.name, "SECRETPATH"):
        assert secret not in text
        assert repr(secret)[1:-1] not in text  # the backslash-doubled form seen in repr()


def size_io(env: Env, name: ArtifactName, *, reported: int, actual: int) -> FakeIO:
    """Fake IO: the artifact reports `reported` bytes and its read yields `actual` bytes in total."""
    path = env.artifact_path(name)
    state = {"left": actual, "requested": 0, "reads": 0}

    def lstat(base, p):
        st = base(p)
        return fake_stat(st, st_size=reported) if p == path else st

    def open_(base, p, flags):
        assert p == path
        return 3  # fake descriptor, never passed to the OS

    def fstat(base, fd):
        return fake_stat(DEFAULT_IO.lstat(path), st_size=reported)

    def read(base, fd, n):
        state["reads"] += 1
        state["requested"] += n
        give = min(n, state["left"])
        state["left"] -= give
        return b"x" * give

    io = FakeIO(lstat=lstat, open=open_, fstat=fstat, read=read, close=lambda base, fd: None)
    io.state = state  # type: ignore[attr-defined]
    return io


# ---------------------------------------------------------------- size bounds


@pytest.mark.parametrize("name", ALL_NAMES, ids=[n.value for n in ALL_NAMES])
def test_exactly_the_limit_is_accepted(env: Env, name: ArtifactName) -> None:
    assert ARTIFACT_LIMIT == 16_777_216 and READ_LIMIT == ARTIFACT_LIMIT + 1
    io = size_io(env, name, reported=ARTIFACT_LIMIT, actual=ARTIFACT_LIMIT)
    result = read_artifact(env.root, env.run, name, io)
    assert result.fault is None and result.reason is None
    assert result.data is not None and len(result.data) == ARTIFACT_LIMIT
    assert result.signature is not None and result.signature[0] == ARTIFACT_LIMIT


@pytest.mark.parametrize("name", ALL_NAMES, ids=[n.value for n in ALL_NAMES])
def test_limit_plus_one_reported_by_stat_is_too_large_without_reading(
    env: Env, name: ArtifactName
) -> None:
    io = size_io(env, name, reported=ARTIFACT_LIMIT + 1, actual=ARTIFACT_LIMIT + 1)
    result = read_artifact(env.root, env.run, name, io)
    assert result.fault is ArtifactFault.TOO_LARGE
    assert result.data is None and result.signature is None
    assert io.state["reads"] == 0  # type: ignore[attr-defined]
    assert files.STATUS_OF[result.fault] is ArtifactStatus.TOO_LARGE


@pytest.mark.parametrize("name", ALL_NAMES, ids=[n.value for n in ALL_NAMES])
def test_limit_plus_one_bytes_read_is_too_large_and_dropped(env: Env, name: ArtifactName) -> None:
    # stat claims exactly the limit, the stream holds one byte more
    io = size_io(env, name, reported=ARTIFACT_LIMIT, actual=ARTIFACT_LIMIT + 1)
    result = read_artifact(env.root, env.run, name, io)
    assert result.fault is ArtifactFault.TOO_LARGE
    assert result.data is None and result.signature is None


def test_growing_file_never_requests_more_than_read_limit(env: Env) -> None:
    name = ArtifactName.EVIDENCE
    path = env.artifact_path(name)
    requested: list[int] = []

    def read(base, fd, n):
        requested.append(n)
        return b"y" * n  # always returns data: the file keeps growing

    io = FakeIO(
        lstat=lambda base, p: fake_stat(base(p), st_size=10) if p == path else base(p),
        open=lambda base, p, flags: 3,
        fstat=lambda base, fd: fake_stat(DEFAULT_IO.lstat(path), st_size=10),
        read=read,
        close=lambda base, fd: None,
    )
    result = read_artifact(env.root, env.run, name, io)
    assert result.fault is ArtifactFault.TOO_LARGE
    assert result.data is None
    assert sum(requested) == READ_LIMIT  # exactly, never more
    assert max(requested) <= READ_CHUNK
    assert len(requested) == READ_LIMIT // READ_CHUNK + 1


def test_descriptor_is_closed_after_read_and_after_failure(env: Env) -> None:
    name = ArtifactName.MANIFEST
    io = size_io(env, name, reported=5, actual=5)
    assert read_artifact(env.root, env.run, name, io).data == b"xxxxx"
    assert [c for c in io.calls if c[0] == "close"] == [("close", 3)]
    # identity failure after open still closes
    path = env.artifact_path(name)
    swapped = FakeIO(
        open=lambda base, p, flags: 3,
        fstat=lambda base, fd: fake_stat(DEFAULT_IO.lstat(path), st_ino=DEFAULT_IO.lstat(path).st_ino + 1),
        close=lambda base, fd: None,
    )
    assert read_artifact(env.root, env.run, name, swapped).fault is ArtifactFault.UNSAFE_PATH
    assert [c for c in swapped.calls if c[0] == "close"] == [("close", 3)]


def test_real_small_artifact_roundtrip_and_signature(env: Env) -> None:
    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST)
    on_disk = (env.runs / env.run_id / "manifest.json").read_bytes()
    assert result.fault is None and result.data == on_disk
    st = os.lstat(env.artifact_path(ArtifactName.MANIFEST))
    assert result.signature == (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)
    assert_no_path(result, env)


# ---------------------------------------------------------------- reparse points (fake IO)


def test_reparse_point_artifact_is_unsafe(env: Env) -> None:
    path = env.artifact_path(ArtifactName.EVIDENCE)
    io = FakeIO(lstat=lambda base, p: fake_stat(base(p), st_file_attributes=REPARSE) if p == path else base(p))
    result = read_artifact(env.root, env.run, ArtifactName.EVIDENCE, io)
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.reason == "symlink_or_reparse_point"
    assert result.data is None
    assert not any(c[0] == "open" for c in io.calls)  # rejected before open
    assert files.STATUS_OF[result.fault] is ArtifactStatus.MISSING


def test_symlink_mode_artifact_is_unsafe(env: Env) -> None:
    path = env.artifact_path(ArtifactName.INSPECT)
    io = FakeIO(lstat=lambda base, p: fake_stat(base(p), st_mode=stat.S_IFLNK | 0o777) if p == path else base(p))
    result = read_artifact(env.root, env.run, ArtifactName.INSPECT, io)
    assert result.fault is ArtifactFault.UNSAFE_PATH
    assert not any(c[0] == "open" for c in io.calls)


def test_reparse_point_run_directory_after_listing_is_unsafe(env: Env) -> None:
    dir_path = env.dir_path
    io = FakeIO(lstat=lambda base, p: fake_stat(base(p), st_file_attributes=REPARSE) if p == dir_path else base(p))
    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, io)
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.reason == "symlink_or_reparse_point"
    assert not any(c[0] == "open" for c in io.calls)


def test_reparse_point_run_directory_is_not_listed_or_found(env: Env) -> None:
    dir_path = env.dir_path
    io = FakeIO(lstat=lambda base, p: fake_stat(base(p), st_file_attributes=REPARSE | 0x10) if p == dir_path else base(p))
    assert list_run_entries(env.root, io) == []
    assert find_run(env.root, env.run_id, io) is None


def test_symlink_mode_run_directory_is_not_listed(env: Env) -> None:
    dir_path = env.dir_path
    io = FakeIO(lstat=lambda base, p: fake_stat(base(p), st_mode=stat.S_IFLNK | 0o777) if p == dir_path else base(p))
    assert list_run_entries(env.root, io) == []


def test_junction_like_directory_with_dir_mode_and_reparse_bit_is_rejected(env: Env) -> None:
    # A Windows junction: S_ISDIR is true and the reparse-point attribute bit is set.
    dir_path = env.dir_path
    real_mode = os.lstat(dir_path).st_mode
    assert stat.S_ISDIR(real_mode)
    io = FakeIO(lstat=lambda base, p: fake_stat(base(p), st_file_attributes=0x10 | REPARSE) if p == dir_path else base(p))
    assert list_run_entries(env.root, io) == []
    assert read_artifact(env.root, env.run, ArtifactName.MANIFEST, io).fault is ArtifactFault.UNSAFE_PATH


# ---------------------------------------------------------------- real symlinks (skip when not creatable)


def _need_symlink(ok: bool) -> None:
    if not ok:
        pytest.skip("this account/platform cannot create symlinks (os.symlink failed); covered by fake-IO reparse tests")


def test_real_symlinked_run_directory_is_not_listed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    RunsBuilder(outside).add_run("B", run_id="b-20261120T211300Z-0000beef")
    builder = RunsBuilder(tmp_path / "runs")
    _need_symlink(builder.try_symlink("b-20261120T211300Z-0000beef", outside / "b-20261120T211300Z-0000beef"))
    root = open_runs_root(tmp_path / "runs")
    assert list_run_entries(root) == []
    assert find_run(root, "b-20261120T211300Z-0000beef") is None


def test_real_symlinked_artifact_is_unsafe(tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside secret")
    env = Env(tmp_path)
    victim = env.runs / env.run_id / "evidence.jsonl"
    victim.unlink()
    _need_symlink(RunsBuilder(victim.parent).try_symlink("evidence.jsonl", outside))
    entry = list_run_entries(env.root)[0]
    result = read_artifact(env.root, entry, ArtifactName.EVIDENCE)
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.data is None
    assert_no_path(result, env)


# ---------------------------------------------------------------- non-regular, directory as artifact


def test_directory_as_artifact_is_not_regular(tmp_path: Path) -> None:
    env = Env(tmp_path)
    (env.runs / env.run_id / "evidence.jsonl").unlink()
    (env.runs / env.run_id / "evidence.jsonl").mkdir()
    entry = list_run_entries(env.root)[0]
    assert ArtifactName.EVIDENCE in entry.present
    result = read_artifact(env.root, entry, ArtifactName.EVIDENCE)
    assert result.fault is ArtifactFault.NOT_REGULAR and result.reason == "not_regular_file"
    assert result.data is None
    assert files.STATUS_OF[result.fault] is ArtifactStatus.MISSING


def test_fake_non_regular_pre_open_entry_is_not_regular(env: Env) -> None:
    path = env.artifact_path(ArtifactName.INSPECT)
    io = FakeIO(lstat=lambda base, p: fake_stat(base(p), st_mode=stat.S_IFIFO | 0o600) if p == path else base(p))
    result = read_artifact(env.root, env.run, ArtifactName.INSPECT, io)
    assert result.fault is ArtifactFault.NOT_REGULAR
    assert not any(c[0] == "open" for c in io.calls)


def test_opened_handle_that_is_not_regular_is_rejected(env: Env) -> None:
    path = env.artifact_path(ArtifactName.MANIFEST)
    io = FakeIO(
        open=lambda base, p, flags: 3,
        fstat=lambda base, fd: fake_stat(DEFAULT_IO.lstat(path), st_mode=stat.S_IFCHR | 0o600),
        close=lambda base, fd: None,
    )
    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, io)
    assert result.fault is ArtifactFault.NOT_REGULAR and result.data is None
    assert not any(c[0] == "read" for c in io.calls)


# ---------------------------------------------------------------- identity swaps


def test_run_directory_identity_change_is_detected(env: Env) -> None:
    dir_path = env.dir_path
    io = FakeIO(lstat=lambda base, p: fake_stat(base(p), st_ino=base(p).st_ino + 7) if p == dir_path else base(p))
    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, io)
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.reason == "identity_changed"
    assert not any(c[0] == "open" for c in io.calls)


def test_run_directory_device_change_is_detected(env: Env) -> None:
    dir_path = env.dir_path
    io = FakeIO(lstat=lambda base, p: fake_stat(base(p), st_dev=base(p).st_dev + 1) if p == dir_path else base(p))
    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, io)
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.reason == "identity_changed"


def test_artifact_swapped_between_lstat_and_fstat_inode(env: Env) -> None:
    path = env.artifact_path(ArtifactName.MANIFEST)
    real = os.lstat(path)
    assert real.st_ino != 0, "this test needs a filesystem with inode numbers"
    io = FakeIO(
        open=lambda base, p, flags: 3,
        fstat=lambda base, fd: fake_stat(real, st_ino=real.st_ino + 1),
        close=lambda base, fd: None,
    )
    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, io)
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.reason == "identity_changed"
    assert result.data is None
    assert not any(c[0] == "read" for c in io.calls)


def test_artifact_swapped_between_lstat_and_fstat_device(env: Env) -> None:
    path = env.artifact_path(ArtifactName.MANIFEST)
    real = os.lstat(path)
    io = FakeIO(
        open=lambda base, p, flags: 3,
        fstat=lambda base, fd: fake_stat(real, st_dev=real.st_dev + 1),
        close=lambda base, fd: None,
    )
    assert read_artifact(env.root, env.run, ArtifactName.MANIFEST, io).reason == "identity_changed"


def test_zero_inode_falls_back_to_size_and_mtime(env: Env) -> None:
    path = env.artifact_path(ArtifactName.MANIFEST)
    real = os.lstat(path)

    def build(post_size: int, post_mtime: int) -> FakeIO:
        return FakeIO(
            lstat=lambda base, p: fake_stat(base(p), st_ino=0) if p == path else base(p),
            open=lambda base, p, flags: 3,
            fstat=lambda base, fd: fake_stat(real, st_ino=0, st_size=post_size, st_mtime_ns=post_mtime),
            read=lambda base, fd, n: b"",
            close=lambda base, fd: None,
        )

    same = read_artifact(env.root, env.run, ArtifactName.MANIFEST, build(real.st_size, real.st_mtime_ns))
    assert same.fault is None  # equal size and mtime: accepted
    for size, mtime in ((real.st_size + 1, real.st_mtime_ns), (real.st_size, real.st_mtime_ns + 1)):
        changed = read_artifact(env.root, env.run, ArtifactName.MANIFEST, build(size, mtime))
        assert changed.fault is ArtifactFault.UNSAFE_PATH and changed.reason == "identity_changed"


def test_second_realpath_comparison_detects_a_swap(env: Env) -> None:
    path = env.artifact_path(ArtifactName.EVIDENCE)
    seen = {"n": 0}

    def realpath(base, p, strict=True):
        if p == path:
            seen["n"] += 1
            if seen["n"] >= 2:
                return base(p, strict) + ".swapped"
        return base(p, strict)

    io = FakeIO(realpath=realpath)
    result = read_artifact(env.root, env.run, ArtifactName.EVIDENCE, io)
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.reason == "identity_changed"
    assert result.data is None
    assert [c for c in io.calls if c[0] == "close"]  # descriptor released


# ---------------------------------------------------------------- containment


def test_artifact_resolving_outside_the_run_directory_is_unsafe(env: Env) -> None:
    path = env.artifact_path(ArtifactName.MANIFEST)
    elsewhere = os.path.join(str(env.tmp_path), "elsewhere", "manifest.json")
    io = FakeIO(realpath=lambda base, p, strict=True: elsewhere if p == path else base(p, strict))
    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, io)
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.reason == "outside_runs_directory"
    assert not any(c[0] == "open" for c in io.calls)
    assert_no_path(result, env)


def test_run_directory_resolving_outside_the_root_is_unsafe(env: Env) -> None:
    outside_dir = os.path.join(str(env.tmp_path), "outside-tree", env.run_id)

    def realpath(base, p, strict=True):
        if p == env.dir_path:
            return outside_dir
        if p == env.artifact_path(ArtifactName.MANIFEST):
            return os.path.join(outside_dir, "manifest.json")
        return base(p, strict)

    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, FakeIO(realpath=realpath))
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.reason == "outside_runs_directory"


def test_runs_evil_sibling_is_not_inside_runs(env: Env) -> None:
    evil_root = env.root.real + "-evil"
    assert not files._is_under(os.path.join(evil_root, env.run_id), env.root.real)
    assert not files._is_under(evil_root, env.root.real)
    assert files._is_under(env.root.real, env.root.real)
    assert files._is_under(os.path.join(env.root.real, env.run_id), env.root.real)

    evil_dir = os.path.join(evil_root, env.run_id)

    def realpath(base, p, strict=True):
        if p == env.dir_path:
            return evil_dir
        if p == env.artifact_path(ArtifactName.MANIFEST):
            return os.path.join(evil_dir, "manifest.json")
        return base(p, strict)

    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, FakeIO(realpath=realpath))
    assert result.fault is ArtifactFault.UNSAFE_PATH and result.reason == "outside_runs_directory"


def test_real_runs_evil_sibling_directory_is_never_listed(tmp_path: Path) -> None:
    builder_evil = RunsBuilder(tmp_path / "runs-evil")
    builder_evil.add_run("B")
    RunsBuilder(tmp_path / "runs").add_run("B", run_id="b-20261120T211300Z-00000001")
    root = open_runs_root(tmp_path / "runs")
    assert [e.run_id for e in list_run_entries(root)] == ["b-20261120T211300Z-00000001"]


# ---------------------------------------------------------------- OSError text is not leaked


@pytest.mark.parametrize("op", ["lstat", "realpath", "open", "fstat", "read"])
def test_oserror_text_with_a_path_yields_unreadable_without_the_path(env: Env, op: str) -> None:
    path = env.artifact_path(ArtifactName.MANIFEST)
    secret = f"[Errno 13] Permission denied: {path!r} SECRETPATH {env.root.real}"

    def boom(base, *args):
        raise OSError(13, secret, path)

    hooks: dict = {op: boom}
    if op in ("fstat", "read"):
        hooks["open"] = lambda base, p, flags: 3
        hooks["close"] = lambda base, fd: None
    if op == "read":
        hooks["fstat"] = lambda base, fd: DEFAULT_IO.lstat(path)
    if op == "lstat":
        hooks["lstat"] = lambda base, p: boom(base) if p == path else base(p)
    if op == "realpath":
        hooks["realpath"] = lambda base, p, strict=True: boom(base) if p == path else base(p, strict)
    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, FakeIO(**hooks))
    assert result.fault is ArtifactFault.UNREADABLE
    assert result.reason == "os_error"
    assert result.data is None and result.signature is None
    assert files.STATUS_OF[result.fault] is ArtifactStatus.UNREADABLE
    assert_no_path(result, env)


def test_file_not_found_during_read_is_missing_without_path(env: Env) -> None:
    path = env.artifact_path(ArtifactName.MANIFEST)

    def gone(base, p):
        if p == path:
            raise FileNotFoundError(2, f"SECRETPATH {path}", path)
        return base(p)

    result = read_artifact(env.root, env.run, ArtifactName.MANIFEST, FakeIO(lstat=gone))
    assert result.fault is ArtifactFault.MISSING and result.reason == "missing"
    assert_no_path(result, env)


def test_artifact_not_in_listing_is_missing_without_io(env: Env) -> None:
    run = list_run_entries(env.root)[0]
    trimmed = type(run)(run_id=run.run_id, present=frozenset({ArtifactName.MANIFEST}), dir_id=run.dir_id)
    io = FakeIO()
    result = read_artifact(env.root, trimmed, ArtifactName.EVIDENCE, io)
    assert result.fault is ArtifactFault.MISSING and result.data is None
    assert io.calls == []


# ---------------------------------------------------------------- exact-case names and reserved names


def test_artifact_name_matching_is_exact_case(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    upper = runs / "x-1"
    upper.mkdir()
    (upper / "MANIFEST.JSON").write_bytes(b"{}")
    root = open_runs_root(runs)
    # MANIFEST.JSON does not count as manifest.json: the directory is not a run.
    assert list_run_entries(root) == []
    assert find_run(root, "x-1") is None

    good = runs / "x-2"
    good.mkdir()
    (good / "manifest.json").write_bytes(b"{}")
    (good / "EVIDENCE.JSONL").write_bytes(b"{}\n")
    (good / "Docker_Inspect.json").write_bytes(b"[]")
    entry = find_run(root, "x-2")
    assert entry is not None
    assert entry.present == frozenset({ArtifactName.MANIFEST})
    for name in (ArtifactName.EVIDENCE, ArtifactName.INSPECT):
        assert read_artifact(root, entry, name).fault is ArtifactFault.MISSING


@pytest.mark.parametrize("run_id", ["con", "CON", "nul", "aux", "com1", "lpt1"])
def test_reserved_names_match_only_by_scandir_name(env: Env, run_id: str) -> None:
    # The id is valid for the run-id pattern, but it is never joined into a path: with no such
    # scandir entry find_run returns None and no lstat/open/read is attempted.
    io = FakeIO()
    assert find_run(env.root, run_id, io) is None
    assert {c[0] for c in io.calls} == {"scandir"}
    assert find_run(env.root, run_id) is None


def test_reserved_name_found_only_when_scandir_lists_it(env: Env) -> None:
    class _Entry:
        name = "con"

    seen: list[str] = []

    def scandir(base, path):
        return contextlib.nullcontext(iter([_Entry()]))

    def lstat(base, p):
        seen.append(p)
        raise FileNotFoundError(2, "gone")

    io = FakeIO(scandir=scandir, lstat=lstat)
    assert find_run(env.root, "con", io) is None  # listed, but the inspected entry is not a run
    assert seen == [os.path.join(env.root.real, "con")]


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "x" * 65, "ok\n", ".hidden", "C:"])
def test_invalid_run_id_raises_before_any_file_access(env: Env, bad: str) -> None:
    io = FakeIO()
    with pytest.raises(RunIdError):
        find_run(env.root, bad, io)
    assert io.calls == []


# ---------------------------------------------------------------- listing entries


def test_empty_runs_directory_lists_nothing(tmp_path: Path) -> None:
    (tmp_path / "runs").mkdir()
    root = open_runs_root(tmp_path / "runs")
    assert list_run_entries(root) == []


def test_listing_skips_loose_files_empty_dirs_and_manifestless_dirs(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    run_a = builder.add_run("A", run_id="a-20261120T211300Z-0000000a")
    run_b = builder.add_run("B", run_id="b-20261120T211300Z-0000000b")
    builder.add_loose_file("notes.txt", b"hello")
    builder.add_loose_file("manifest.json", b"{}")
    builder.add_loose_file("c-20261120T211300Z-0000000c", b"a file named like a run id")
    builder.add_empty_dir("empty-dir")
    builder.add_empty_dir("not a valid id")
    nomanifest = tmp_path / "runs" / "nomanifest-1"
    nomanifest.mkdir()
    (nomanifest / "evidence.jsonl").write_bytes(b"")
    (nomanifest / "docker_inspect.json").write_bytes(b"[]")

    root = open_runs_root(tmp_path / "runs")
    entries = list_run_entries(root)
    assert [e.run_id for e in entries] == sorted([run_a, run_b])
    for entry in entries:
        assert entry.present == frozenset(ALL_NAMES)
        st = os.lstat(tmp_path / "runs" / entry.run_id)
        assert entry.dir_id == (st.st_dev, st.st_ino)
    assert find_run(root, "nomanifest-1") is None
    assert find_run(root, "empty-dir") is None
    assert find_run(root, "notes.txt") is None
    assert find_run(root, "manifest.json") is None
    assert find_run(root, "does-not-exist") is None
    found = find_run(root, run_a)
    assert found is not None and found.run_id == run_a


def test_listing_present_set_reflects_missing_artifacts(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    only_manifest = builder.add_run("B", run_id="b-20261120T211300Z-00000001", inspect=None)
    (tmp_path / "runs" / only_manifest / "evidence.jsonl").unlink()
    root = open_runs_root(tmp_path / "runs")
    [entry] = list_run_entries(root)
    assert entry.present == frozenset({ArtifactName.MANIFEST})
    result = read_artifact(root, entry, ArtifactName.EVIDENCE)
    assert result.fault is ArtifactFault.MISSING and result.data is None


def test_manifest_that_is_a_directory_still_counts_as_present(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run = runs / "b-20261120T211300Z-0000dead"
    (run / "manifest.json").mkdir(parents=True)
    root = open_runs_root(runs)
    [entry] = list_run_entries(root)
    assert entry.run_id == run.name
    assert ArtifactName.MANIFEST in entry.present
    result = read_artifact(root, entry, ArtifactName.MANIFEST)
    assert result.fault is ArtifactFault.NOT_REGULAR and result.data is None


def test_listing_is_sorted_by_name_and_deterministic(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    ids = [builder.add_run("B", run_id=f"b-20261120T211300Z-{n:08x}") for n in (3, 1, 2)]
    root = open_runs_root(tmp_path / "runs")
    first = [e.run_id for e in list_run_entries(root)]
    assert first == sorted(ids)
    assert first == [e.run_id for e in list_run_entries(root)]


def test_scan_error_of_the_runs_directory_propagates(env: Env) -> None:
    def scandir(base, path):
        raise PermissionError(13, "SECRETPATH denied", path)

    with pytest.raises(OSError):
        list_run_entries(env.root, FakeIO(scandir=scandir))


def test_unreadable_run_directory_is_skipped_not_fatal(env: Env) -> None:
    dir_path = env.dir_path

    def scandir(base, path):
        if path == dir_path:
            raise PermissionError(13, "SECRETPATH denied", path)
        return base(path)

    assert list_run_entries(env.root, FakeIO(scandir=scandir)) == []


def test_listing_and_reading_do_not_modify_the_runs_directory(tmp_path: Path) -> None:
    builder = RunsBuilder(tmp_path / "runs")
    builder.add_run("B", run_id="b-20261120T211300Z-00000001")
    builder.add_loose_file("loose.txt", b"x")
    before = builder.snapshot()
    root = open_runs_root(tmp_path / "runs")
    for entry in list_run_entries(root):
        for name in ArtifactName:
            read_artifact(root, entry, name)
    assert builder.snapshot() == before


# ---------------------------------------------------------------- runs root


def test_open_runs_root_rejects_missing_and_non_directory(tmp_path: Path) -> None:
    with pytest.raises(RunsRootError):
        open_runs_root(tmp_path / "does-not-exist")
    afile = tmp_path / "afile"
    afile.write_bytes(b"x")
    with pytest.raises(RunsRootError):
        open_runs_root(afile)


def test_open_runs_root_label_is_the_final_component(tmp_path: Path) -> None:
    (tmp_path / "myruns").mkdir()
    root = open_runs_root(tmp_path / "myruns")
    assert root.label == "myruns"
    assert os.path.isabs(root.real)
    assert root.real not in root.label
