"""Run_Manifest dataclasses, atomic read/write, inspect excerpt, and run id generation.

Requirements 9.1, 10.1, 11.1, 11.13, 11.14, 12.1, 12.2, 12.7.

Files are written in binary mode with explicit UTF-8 and LF line endings (no newline
translation on Windows), through a temp file in the same directory, `fsync`, and
`os.replace`. Stored paths always use forward slashes (`as_posix`). Raw `docker inspect`
output is saved byte for byte. Standard library only.

Test seams: `generate_run_id` takes `clock` and `rand_hex`; the write functions take
`fsync` and `replace`.
"""
from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from coroner.evidence.schema import RUN_ID_RE

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILE_NAME = "manifest.json"
OUTCOMES = ("passed", "failed", "aborted")
MAX_RUN_ID_ATTEMPTS = 100
_EXPERIMENT_RE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_HEX = frozenset("0123456789abcdef")


class ManifestError(ValueError):
    """A manifest is malformed, or writing it would be unsafe."""


class ManifestConflictError(ManifestError):
    """An unrelated manifest already exists at the target path."""


class InspectError(ValueError):
    """`docker inspect` output lacks a required field or has a wrong type."""


class RunIdError(ValueError):
    """An invalid experiment or run identifier."""


class RunIdExhaustedError(RuntimeError):
    """No unused run id was found within the attempt bound."""


# ---------------------------------------------------------------- paths


def posix_path(value: str | PurePath) -> str:
    """Portable stored form of a path: forward slashes, whatever the host OS."""
    if isinstance(value, PurePath):
        return value.as_posix()
    if "\\" in value:
        return PureWindowsPath(value).as_posix()
    return value


def _opt_posix(value: str | PurePath | None) -> str | None:
    return None if value is None else posix_path(value)


# ---------------------------------------------------------------- run ids


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_rand_hex() -> str:
    return secrets.token_hex(4)


def generate_run_id(
    experiment: str,
    existing: Collection[str] = (),
    *,
    runs_dir: str | PurePath | None = None,
    clock: Callable[[], datetime] | None = None,
    rand_hex: Callable[[], str] | None = None,
    max_attempts: int = MAX_RUN_ID_ATTEMPTS,
) -> str:
    """Return `<experiment>-<UTC yyyymmddThhmmssZ>-<8 lowercase hex>`.

    The result matches `RUN_ID_RE`, is absent from `existing` (compared case-insensitively),
    and, when `runs_dir` is given, has no directory or file of that name there. Collisions
    retry with fresh randomness, at most `max_attempts` times, then raise RunIdExhaustedError.
    `experiment` is lowercased and must be 1 to 16 characters of a-z and 0-9.
    """
    exp = experiment.lower() if isinstance(experiment, str) else experiment
    if (
        not isinstance(exp, str)
        or not 1 <= len(exp) <= 16
        or not set(exp) <= _EXPERIMENT_RE_CHARS
    ):
        raise RunIdError(f"invalid experiment {experiment!r}: expected 1 to 16 of a-z, 0-9")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    known = {str(item).lower() for item in existing}
    base = Path(runs_dir) if runs_dir is not None else None
    get_clock = clock or _default_clock
    get_hex = rand_hex or _default_rand_hex
    for _ in range(max_attempts):
        now = get_clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RunIdError("clock must return an aware datetime")
        stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = get_hex()
        if not isinstance(suffix, str) or len(suffix) != 8 or not set(suffix) <= _HEX:
            raise RunIdError(f"rand_hex must return 8 lowercase hex characters, got {suffix!r}")
        candidate = f"{exp}-{stamp}-{suffix}"
        if RUN_ID_RE.fullmatch(candidate) is None:  # defensive; cannot happen by construction
            raise RunIdError(f"generated id {candidate!r} does not match {RUN_ID_RE.pattern}")
        if candidate.lower() in known:
            continue
        if base is not None and (base / candidate).exists():
            continue
        return candidate
    raise RunIdExhaustedError(f"no unused run id after {max_attempts} attempts")


def validate_run_id_for_path(run_id: object) -> str:
    """Return `run_id` if it matches `RUN_ID_RE` (full match), else raise RunIdError."""
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise RunIdError(f"invalid run_id {run_id!r}: must match {RUN_ID_RE.pattern}")
    return run_id


# ---------------------------------------------------------------- inspect


@dataclass(frozen=True)
class InspectExcerpt:
    """Exactly the five specified `docker inspect` fields."""

    id: str
    status: str
    oom_killed: bool
    exit_code: int
    memory: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "Id": self.id,
            "State": {
                "Status": self.status,
                "OOMKilled": self.oom_killed,
                "ExitCode": self.exit_code,
            },
            "HostConfig": {"Memory": self.memory},
        }

    @classmethod
    def from_dict(cls, data: object) -> "InspectExcerpt":
        return _excerpt_from_obj(data)


def _excerpt_from_obj(obj: object) -> InspectExcerpt:
    if isinstance(obj, list):
        if len(obj) != 1:
            raise InspectError(f"expected one container in inspect output, got {len(obj)}")
        obj = obj[0]
    if not isinstance(obj, Mapping):
        raise InspectError("inspect output is not a JSON object")
    cid = obj.get("Id")
    if not isinstance(cid, str) or len(cid) != 64:
        raise InspectError("Id must be the full 64-character container id")
    state = obj.get("State")
    host = obj.get("HostConfig")
    if not isinstance(state, Mapping):
        raise InspectError("State missing or not an object")
    if not isinstance(host, Mapping):
        raise InspectError("HostConfig missing or not an object")
    status = state.get("Status")
    oom = state.get("OOMKilled")
    code = state.get("ExitCode")
    mem = host.get("Memory")
    if not isinstance(status, str):
        raise InspectError("State.Status missing or not a string")
    if not isinstance(oom, bool):
        raise InspectError("State.OOMKilled missing or not a boolean")
    if not isinstance(code, int) or isinstance(code, bool):
        raise InspectError("State.ExitCode missing or not an integer")
    if not isinstance(mem, int) or isinstance(mem, bool):
        raise InspectError("HostConfig.Memory missing or not an integer")
    return InspectExcerpt(id=cid, status=status, oom_killed=oom, exit_code=code, memory=mem)


def extract_inspect_excerpt(raw: bytes | str) -> InspectExcerpt:
    """Extract the excerpt from raw `docker inspect` output (a one-element list or an object)."""
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InspectError(f"inspect output is not valid JSON: {exc}") from exc
    return _excerpt_from_obj(obj)


# ---------------------------------------------------------------- manifest model


@dataclass(frozen=True)
class Failure:
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class Expected:
    exit_code: int
    oom_killed: bool

    def to_dict(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "oom_killed": self.oom_killed}


@dataclass(frozen=True)
class ContainerState:
    status: str
    exit_code: int
    oom_killed: bool
    container_id: str
    host_config_memory_bytes: int
    inspect_time: str
    raw_inspect_path: str
    raw_inspect_excerpt: InspectExcerpt

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_inspect_path", posix_path(self.raw_inspect_path))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "oom_killed": self.oom_killed,
            "container_id": self.container_id,
            "host_config_memory_bytes": self.host_config_memory_bytes,
            "inspect_time": self.inspect_time,
            "raw_inspect_path": self.raw_inspect_path,
            "raw_inspect_excerpt": self.raw_inspect_excerpt.to_dict(),
        }

    @classmethod
    def from_inspect(
        cls, raw: bytes | str, inspect_time: str, raw_inspect_path: str | PurePath
    ) -> "ContainerState":
        ex = extract_inspect_excerpt(raw)
        return cls(
            status=ex.status,
            exit_code=ex.exit_code,
            oom_killed=ex.oom_killed,
            container_id=ex.id,
            host_config_memory_bytes=ex.memory,
            inspect_time=inspect_time,
            raw_inspect_path=posix_path(raw_inspect_path),
            raw_inspect_excerpt=ex,
        )


@dataclass(frozen=True)
class EvidenceRef:
    container_path: str
    host_copy_path: str
    record_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "container_path", posix_path(self.container_path))
        object.__setattr__(self, "host_copy_path", posix_path(self.host_copy_path))

    def to_dict(self) -> dict[str, Any]:
        return {
            "container_path": self.container_path,
            "host_copy_path": self.host_copy_path,
            "record_count": self.record_count,
        }


_KNOWN_KEYS = (
    "schema_version",
    "experiment",
    "run_id",
    "outcome",
    "failure",
    "start_time",
    "end_time",
    "durability_level",
    "termination_description",
    "expected",
    "container_state",
    "evidence",
)


@dataclass
class RunManifest:
    """A Run_Manifest. Other top-level blocks (memory, leak, times, checks, ...) live in `extra`
    and are written at the top level, so they survive a read/write cycle unchanged."""

    experiment: str
    run_id: str
    outcome: str
    start_time: str
    end_time: str
    durability_level: str
    termination_description: str
    expected: Expected
    failure: Failure | None = None
    container_state: ContainerState | None = None
    evidence: EvidenceRef | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        clash = set(self.extra) & set(_KNOWN_KEYS)
        if clash:
            raise ManifestError(f"extra must not redefine core keys: {sorted(clash)}")
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "experiment": self.experiment,
            "run_id": self.run_id,
            "outcome": self.outcome,
            "failure": None if self.failure is None else self.failure.to_dict(),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "durability_level": self.durability_level,
            "termination_description": self.termination_description,
            "expected": self.expected.to_dict(),
            "container_state": None if self.container_state is None else self.container_state.to_dict(),
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
        }
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: object) -> "RunManifest":
        if not isinstance(data, Mapping):
            raise ManifestError("manifest is not a JSON object")
        missing = [k for k in _KNOWN_KEYS if k not in data]
        if missing:
            raise ManifestError(f"manifest missing fields: {missing}")
        if data["schema_version"] != MANIFEST_SCHEMA_VERSION or isinstance(
            data["schema_version"], bool
        ):
            raise ManifestError(f"unsupported schema_version {data['schema_version']!r}")
        try:
            validate_run_id_for_path(data["run_id"])
        except RunIdError as exc:
            raise ManifestError(str(exc)) from exc
        if data["outcome"] not in OUTCOMES:
            raise ManifestError(f"invalid outcome {data['outcome']!r}")
        for key in ("experiment", "start_time", "end_time", "durability_level", "termination_description"):
            if not isinstance(data[key], str):
                raise ManifestError(f"{key} must be a string")
        try:
            failure = _failure_from(data["failure"])
            expected = _expected_from(data["expected"])
            state = _state_from(data["container_state"])
            evidence = _evidence_from(data["evidence"])
        except (KeyError, TypeError, InspectError) as exc:
            raise ManifestError(f"malformed manifest block: {exc!r}") from exc
        return cls(
            schema_version=MANIFEST_SCHEMA_VERSION,
            experiment=data["experiment"],
            run_id=data["run_id"],
            outcome=data["outcome"],
            failure=failure,
            start_time=data["start_time"],
            end_time=data["end_time"],
            durability_level=data["durability_level"],
            termination_description=data["termination_description"],
            expected=expected,
            container_state=state,
            evidence=evidence,
            extra={k: v for k, v in data.items() if k not in _KNOWN_KEYS},
        )


def _need(obj: Mapping[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    value = obj[key]
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is int):
        raise TypeError(f"{key} has wrong type")
    return value


def _failure_from(obj: object) -> Failure | None:
    if obj is None:
        return None
    if not isinstance(obj, Mapping):
        raise TypeError("failure is not an object")
    return Failure(code=_need(obj, "code", str), message=_need(obj, "message", str))


def _expected_from(obj: object) -> Expected:
    if not isinstance(obj, Mapping):
        raise TypeError("expected is not an object")
    return Expected(exit_code=_need(obj, "exit_code", int), oom_killed=_need(obj, "oom_killed", bool))


def _state_from(obj: object) -> ContainerState | None:
    if obj is None:
        return None
    if not isinstance(obj, Mapping):
        raise TypeError("container_state is not an object")
    return ContainerState(
        status=_need(obj, "status", str),
        exit_code=_need(obj, "exit_code", int),
        oom_killed=_need(obj, "oom_killed", bool),
        container_id=_need(obj, "container_id", str),
        host_config_memory_bytes=_need(obj, "host_config_memory_bytes", int),
        inspect_time=_need(obj, "inspect_time", str),
        raw_inspect_path=_need(obj, "raw_inspect_path", str),
        raw_inspect_excerpt=_excerpt_from_obj(obj["raw_inspect_excerpt"]),
    )


def _evidence_from(obj: object) -> EvidenceRef | None:
    if obj is None:
        return None
    if not isinstance(obj, Mapping):
        raise TypeError("evidence is not an object")
    return EvidenceRef(
        container_path=_need(obj, "container_path", str),
        host_copy_path=_need(obj, "host_copy_path", str),
        record_count=_need(obj, "record_count", int),
    )


# ---------------------------------------------------------------- serialization and files


def serialize_manifest(manifest: RunManifest) -> bytes:
    """UTF-8 JSON, LF line endings, trailing newline, no BOM."""
    text = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def deserialize_manifest(data: bytes) -> RunManifest:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"manifest is not valid UTF-8 JSON: {exc}") from exc
    return RunManifest.from_dict(obj)


def manifest_path(runs_dir: str | PurePath, run_id: str) -> Path:
    """`<runs_dir>/<run_id>/manifest.json` for a valid run id."""
    return Path(runs_dir) / validate_run_id_for_path(run_id) / MANIFEST_FILE_NAME


def atomic_write_bytes(
    path: str | PurePath,
    data: bytes,
    *,
    fsync: Callable[[int], None] = os.fsync,
    replace: Callable[[Any, Any], None] = os.replace,
) -> None:
    """Write `data` to `path` atomically: temp file in the same directory, fsync, os.replace.

    Binary mode, so no newline translation. On any failure the temp file is removed and the
    previous file at `path`, if any, is untouched.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        with open(tmp, "xb") as handle:
            handle.write(data)
            handle.flush()
            fsync(handle.fileno())
        replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def save_raw_inspect(
    path: str | PurePath,
    raw: bytes,
    *,
    fsync: Callable[[int], None] = os.fsync,
    replace: Callable[[Any, Any], None] = os.replace,
) -> None:
    """Save raw `docker inspect` output byte for byte."""
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("raw inspect output must be bytes")
    atomic_write_bytes(path, bytes(raw), fsync=fsync, replace=replace)


def read_manifest(path: str | PurePath) -> RunManifest:
    return deserialize_manifest(Path(path).read_bytes())


def write_manifest(
    runs_dir: str | PurePath,
    manifest: RunManifest,
    *,
    fsync: Callable[[int], None] = os.fsync,
    replace: Callable[[Any, Any], None] = os.replace,
) -> Path:
    """Atomically write `<runs_dir>/<run_id>/manifest.json` and return its path.

    Replacing a manifest with the same run id is allowed. An existing manifest for another run
    id, or one that cannot be read, is never overwritten (ManifestConflictError).
    """
    target = manifest_path(runs_dir, manifest.run_id)
    payload = serialize_manifest(manifest)  # validates before touching disk
    deserialize_manifest(payload)
    if target.exists():
        try:
            previous = read_manifest(target)
        except (ManifestError, OSError) as exc:
            raise ManifestConflictError(f"refusing to overwrite unreadable {target}: {exc}") from exc
        if previous.run_id != manifest.run_id:
            raise ManifestConflictError(
                f"{target} belongs to run {previous.run_id!r}, not {manifest.run_id!r}"
            )
    atomic_write_bytes(target, payload, fsync=fsync, replace=replace)
    return target
