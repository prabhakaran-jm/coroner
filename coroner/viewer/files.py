"""Safe, bounded, read-only access to run artifacts.

Every artifact is read through ``read_artifact``. The caller receives bytes
(never a path). Run directories are found by listing the resolved runs
directory and matching entry names exactly; a request id is never joined into
a path. No value returned from this module contains an absolute host path.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import Enum
from typing import Any, ContextManager, Iterator, Protocol

from coroner.runner.manifest import RunIdError, validate_run_id_for_path

__all__ = [
    "ARTIFACT_LIMIT",
    "READ_LIMIT",
    "READ_CHUNK",
    "ArtifactName",
    "ArtifactFault",
    "ArtifactStatus",
    "STATUS_OF",
    "RunsRoot",
    "RunsRootError",
    "RunEntry",
    "ArtifactRead",
    "ArtifactIO",
    "DEFAULT_IO",
    "RunIdError",
    "open_runs_root",
    "list_run_entries",
    "find_run",
    "read_artifact",
    "artifact_signature",
]

ARTIFACT_LIMIT = 16_777_216  # 16 MiB; a size exactly equal to the limit is allowed
READ_LIMIT = ARTIFACT_LIMIT + 1  # the most bytes ever requested in total
READ_CHUNK = 1_048_576

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ArtifactName(str, Enum):
    MANIFEST = "manifest.json"
    EVIDENCE = "evidence.jsonl"
    INSPECT = "docker_inspect.json"


class ArtifactFault(str, Enum):
    MISSING = "missing"
    NOT_REGULAR = "not_regular_file"
    UNSAFE_PATH = "unsafe_path"
    TOO_LARGE = "too_large"
    UNREADABLE = "unreadable"
    INVALID = "invalid"  # set by loader.py after parsing fails


class ArtifactStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    INVALID = "invalid"
    TOO_LARGE = "too_large"
    NOT_READ = "not_read"  # assigned by loader.py only; never produced here


STATUS_OF = {
    ArtifactFault.MISSING: ArtifactStatus.MISSING,
    ArtifactFault.NOT_REGULAR: ArtifactStatus.MISSING,
    ArtifactFault.UNSAFE_PATH: ArtifactStatus.MISSING,
    ArtifactFault.TOO_LARGE: ArtifactStatus.TOO_LARGE,
    ArtifactFault.UNREADABLE: ArtifactStatus.UNREADABLE,
    ArtifactFault.INVALID: ArtifactStatus.INVALID,
}


class RunsRootError(Exception):
    """The runs directory is missing or not a directory (start-up error)."""


@dataclass(frozen=True)
class RunsRoot:
    real: str  # resolved absolute path; never serialised or logged
    label: str  # display label (final component of the runs directory)


@dataclass(frozen=True)
class RunEntry:
    run_id: str
    present: frozenset[ArtifactName]
    dir_id: tuple[int, int]  # (st_dev, st_ino) of the run directory at listing time


@dataclass(frozen=True)
class ArtifactRead:
    name: ArtifactName
    fault: ArtifactFault | None
    reason: str | None
    data: bytes | None
    signature: tuple[int, int, int, int] | None  # (size, mtime_ns, dev, ino)


class ArtifactIO(Protocol):
    """Filesystem seam. Tests inject fakes; production uses ``DEFAULT_IO``."""

    def scandir(self, path: str) -> ContextManager[Iterator[os.DirEntry[str]]]: ...
    def lstat(self, path: str) -> os.stat_result: ...
    def open(self, path: str, flags: int) -> int: ...
    def fstat(self, fd: int) -> os.stat_result: ...
    def read(self, fd: int, n: int) -> bytes: ...
    def close(self, fd: int) -> None: ...
    def realpath(self, path: str, strict: bool = True) -> str: ...


class _DefaultIO:
    def scandir(self, path: str) -> Any:
        return os.scandir(path)

    def lstat(self, path: str) -> os.stat_result:
        return os.lstat(path)

    def open(self, path: str, flags: int) -> int:
        return os.open(path, flags)

    def fstat(self, fd: int) -> os.stat_result:
        return os.fstat(fd)

    def read(self, fd: int, n: int) -> bytes:
        return os.read(fd, n)

    def close(self, fd: int) -> None:
        os.close(fd)

    def realpath(self, path: str, strict: bool = True) -> str:
        return os.path.realpath(path, strict=strict)


DEFAULT_IO: ArtifactIO = _DefaultIO()

_ARTIFACT_NAMES = {member.value: member for member in ArtifactName}


def _open_flags() -> int:
    flags = os.O_RDONLY
    for flag in ("O_BINARY", "O_NOFOLLOW", "O_CLOEXEC", "O_NOINHERIT", "O_NONBLOCK"):
        flags |= getattr(os, flag, 0)
    return flags


def _is_link_or_reparse(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _is_under(child: str, parent: str) -> bool:
    """True when ``child`` equals ``parent`` or lies below it (separator boundary)."""
    child_n = os.path.normcase(child)
    parent_n = os.path.normcase(parent)
    if child_n == parent_n:
        return True
    boundary = parent_n if parent_n.endswith(os.sep) else parent_n + os.sep
    return child_n.startswith(boundary)


def open_runs_root(runs_dir: str | os.PathLike[str]) -> RunsRoot:
    """Resolve the runs directory once. Raises ``RunsRootError`` when unusable.

    The message echoes the operator's own ``--runs-dir`` value only.
    """
    text = os.fspath(runs_dir)
    try:
        real = os.path.realpath(text, strict=True)
        is_dir = stat.S_ISDIR(os.stat(real).st_mode)
    except (OSError, ValueError):
        raise RunsRootError(f"runs directory not found or not a directory: {text}") from None
    if not is_dir:
        raise RunsRootError(f"runs directory not found or not a directory: {text}")
    if os.path.isabs(text) or os.path.splitdrive(text)[0]:
        label = os.path.basename(os.path.normpath(text)) or os.path.basename(real)
    else:
        label = text
    return RunsRoot(real=real, label=label)


def _inspect_run_dir(root: RunsRoot, name: str, io: ArtifactIO) -> RunEntry | None:
    """Return a RunEntry for ``name`` (an exact scandir name) or None if it is not a run."""
    try:
        validate_run_id_for_path(name)
    except RunIdError:
        return None
    dir_path = os.path.join(root.real, name)
    try:
        st = io.lstat(dir_path)
    except OSError:
        return None
    if _is_link_or_reparse(st) or not stat.S_ISDIR(st.st_mode):
        return None
    present: set[ArtifactName] = set()
    try:
        with io.scandir(dir_path) as it:
            for child in it:
                member = _ARTIFACT_NAMES.get(child.name)
                if member is not None:
                    present.add(member)
    except OSError:
        return None
    if ArtifactName.MANIFEST not in present:
        return None
    return RunEntry(run_id=name, present=frozenset(present), dir_id=(st.st_dev, st.st_ino))


def list_run_entries(root: RunsRoot, io: ArtifactIO = DEFAULT_IO) -> list[RunEntry]:
    """List run directories (those holding a ``manifest.json`` entry), sorted by id.

    An ``OSError`` from scanning the runs directory itself propagates; callers
    must not echo its message.
    """
    with io.scandir(root.real) as it:
        names = sorted(entry.name for entry in it)
    entries: list[RunEntry] = []
    for name in names:
        entry = _inspect_run_dir(root, name, io)
        if entry is not None:
            entries.append(entry)
    return entries


def find_run(root: RunsRoot, run_id: str, io: ArtifactIO = DEFAULT_IO) -> RunEntry | None:
    """Find one run by exact name. Raises ``RunIdError`` before any file access.

    The id is only compared with names returned by ``scandir``; it is never
    joined into a path.
    """
    validate_run_id_for_path(run_id)
    with io.scandir(root.real) as it:
        found = any(entry.name == run_id for entry in it)
    if not found:
        return None
    return _inspect_run_dir(root, run_id, io)


def _fail(
    name: ArtifactName, fault: ArtifactFault, reason: str
) -> ArtifactRead:
    return ArtifactRead(name=name, fault=fault, reason=reason, data=None, signature=None)


def _same_identity(pre: os.stat_result, post: os.stat_result) -> bool:
    if pre.st_ino != 0:
        return (post.st_dev, post.st_ino) == (pre.st_dev, pre.st_ino)
    return (post.st_size, post.st_mtime_ns) == (pre.st_size, pre.st_mtime_ns)


def read_artifact(
    root: RunsRoot,
    run: RunEntry,
    name: ArtifactName,
    io: ArtifactIO = DEFAULT_IO,
) -> ArtifactRead:
    """Read one artifact with containment checks and a hard byte bound."""
    if name not in run.present:
        return _fail(name, ArtifactFault.MISSING, "missing")
    try:
        return _read_checked(root, run, name, io)
    except FileNotFoundError:
        return _fail(name, ArtifactFault.MISSING, "missing")
    except OSError:
        # The message and filename can hold absolute paths: discard them.
        return _fail(name, ArtifactFault.UNREADABLE, "os_error")


def artifact_signature(
    root: RunsRoot,
    run: RunEntry,
    name: ArtifactName,
    io: ArtifactIO = DEFAULT_IO,
) -> tuple[int, int, int, int] | None:
    """Signature ``(size, mtime_ns, dev, ino)`` of one artifact from ``lstat`` only.

    Never opens or reads the file. Returns None when the artifact is not present,
    is not a regular file, is a link or reparse point, the run directory identity
    changed, or any OS error occurs.
    """
    if name not in run.present:
        return None
    try:
        dir_path = os.path.join(root.real, run.run_id)
        dir_st = io.lstat(dir_path)
        if (dir_st.st_dev, dir_st.st_ino) != run.dir_id:
            return None
        if _is_link_or_reparse(dir_st) or not stat.S_ISDIR(dir_st.st_mode):
            return None
        st = io.lstat(os.path.join(dir_path, name.value))
        if _is_link_or_reparse(st) or not stat.S_ISREG(st.st_mode):
            return None
        return (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)
    except OSError:
        return None


def _read_checked(
    root: RunsRoot, run: RunEntry, name: ArtifactName, io: ArtifactIO
) -> ArtifactRead:
    dir_path = os.path.join(root.real, run.run_id)

    # 2. Run directory identity and reparse re-check.
    dir_st = io.lstat(dir_path)
    if (dir_st.st_dev, dir_st.st_ino) != run.dir_id:
        return _fail(name, ArtifactFault.UNSAFE_PATH, "identity_changed")
    if _is_link_or_reparse(dir_st) or not stat.S_ISDIR(dir_st.st_mode):
        return _fail(name, ArtifactFault.UNSAFE_PATH, "symlink_or_reparse_point")

    # 3. Pre-open lstat of the artifact.
    path = os.path.join(dir_path, name.value)
    pre = io.lstat(path)
    if _is_link_or_reparse(pre):
        return _fail(name, ArtifactFault.UNSAFE_PATH, "symlink_or_reparse_point")
    if not stat.S_ISREG(pre.st_mode):
        return _fail(name, ArtifactFault.NOT_REGULAR, "not_regular_file")

    # 4. Containment of the resolved path.
    real_p = io.realpath(path, strict=True)
    real_dir = io.realpath(dir_path, strict=True)
    if os.path.normcase(os.path.dirname(real_p)) != os.path.normcase(real_dir) or not _is_under(
        real_dir, root.real
    ):
        return _fail(name, ArtifactFault.UNSAFE_PATH, "outside_runs_directory")

    # 5. Early size rejection (does not replace the bounded read).
    if pre.st_size > ARTIFACT_LIMIT:
        return _fail(name, ArtifactFault.TOO_LARGE, "too_large")

    # 6. Open read-only, no-follow where available.
    fd = io.open(path, _open_flags())
    try:
        # 7. The opened handle must be a regular file.
        post = io.fstat(fd)
        if not stat.S_ISREG(post.st_mode):
            return _fail(name, ArtifactFault.NOT_REGULAR, "not_regular_file")
        # 8. Handle identity must match the checked entry.
        if not _same_identity(pre, post):
            return _fail(name, ArtifactFault.UNSAFE_PATH, "identity_changed")
        # 9. Second realpath comparison.
        if io.realpath(path, strict=True) != real_p:
            return _fail(name, ArtifactFault.UNSAFE_PATH, "identity_changed")
        # 10. Chunked read of at most READ_LIMIT bytes in total.
        chunks: list[bytes] = []
        total = 0
        while total < READ_LIMIT:
            chunk = io.read(fd, min(READ_CHUNK, READ_LIMIT - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        # 11.
        io.close(fd)

    # 12. Over the limit: drop the bytes.
    if total > ARTIFACT_LIMIT:
        chunks.clear()
        return _fail(name, ArtifactFault.TOO_LARGE, "too_large")
    data = b"".join(chunks)

    # 13. Signature for the cache.
    final = io.lstat(path)
    signature = (final.st_size, final.st_mtime_ns, final.st_dev, final.st_ino)
    return ArtifactRead(name=name, fault=None, reason=None, data=data, signature=signature)
