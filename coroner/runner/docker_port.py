"""Docker access for the Experiment_Runner (Requirements 16.1, 16.2, 16.3).

`DockerPort` is the seam the experiments depend on. `DockerCli` implements it with
`subprocess.run` and argv lists only: never `shell=True`, never POSIX inline `VAR=value`,
never a string command. Compose variables travel through `env=`, a merged copy of the
base environment (normally `os.environ`), which is never mutated. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

COMPOSE_PROJECT = "coroner"
DEFAULT_COMPOSE_PROJECT = COMPOSE_PROJECT
MAX_COMPOSE_PROJECT_LEN = 63
DEFAULT_TIMEOUT_S = 60.0
COMPOSE_UP_TIMEOUT_S = 300.0

_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
# Docker Compose: lowercase letters, digits, hyphens, underscores; starts with a lowercase letter or digit. Compose
# documents no length limit; 63 (one DNS label) is this runner's own conservative cap.
_COMPOSE_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_SIGNAL_RE = re.compile(r"[A-Za-z0-9]{1,16}")


class DockerError(Exception):
    """Base class for Docker CLI failures."""


class DockerUnavailableError(DockerError):
    """The docker executable is missing or cannot be started."""


class DockerTimeoutError(DockerError):
    """A docker command did not finish within its timeout."""


class DockerCommandError(DockerError):
    """A docker command exited with a non-zero status."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        super().__init__(f"{' '.join(argv)} exited with {returncode}: {stderr.strip()}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


class DockerOutputError(DockerError):
    """A docker command succeeded but its output was not what the runner needs."""


@dataclass(frozen=True)
class InspectResult:
    """`docker inspect` output: raw bytes kept unchanged, parsed JSON, and the UTC inspect time."""

    raw: bytes
    parsed: Any
    inspect_time: datetime


class DockerPort(Protocol):
    def compose_up(self, service: str, *, env: Mapping[str, str], no_deps: bool, recreate: bool) -> None: ...
    def container_id(self, service: str) -> str: ...
    def inspect(self, container_id: str) -> InspectResult: ...
    def kill(self, container_id: str, signal: str) -> None: ...
    def available(self) -> bool: ...


def build_child_env(base: Mapping[str, str], overrides: Mapping[str, str]) -> dict[str, str]:
    """Return a new dict: every base variable, with the overrides winning. Inputs are not mutated."""
    merged = dict(base)
    merged.update(overrides)
    return merged


def _as_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value)


def _as_text(value: Any) -> str:
    return _as_bytes(value).decode("utf-8", errors="replace")


def _validate_container_id(value: object) -> str:
    if not isinstance(value, str) or _CONTAINER_ID_RE.fullmatch(value) is None:
        raise ValueError(f"invalid container id {value!r}: expected 64 lowercase hex characters")
    return value


def validate_compose_project(name: object) -> str:
    """Return `name` if it is a valid Docker Compose project name; otherwise raise ValueError (never falls back)."""
    if (
        not isinstance(name, str)
        or len(name) > MAX_COMPOSE_PROJECT_LEN
        or _COMPOSE_PROJECT_RE.fullmatch(name) is None
        or not name.isascii()
    ):
        raise ValueError(
            f"invalid Docker Compose project name {name!r}: use 1 to {MAX_COMPOSE_PROJECT_LEN} lowercase letters, "
            "digits, hyphens or underscores, starting with a lowercase letter or digit (for example 'coroner-m07')"
        )
    return name


class DockerCli:
    """`DockerPort` over the docker CLI. `run` is injectable (a `subprocess.run` replacement).

    `project` is the Docker Compose project name used by every compose call (default `coroner`).
    """

    def __init__(
        self,
        *,
        run: Callable[..., Any] = subprocess.run,
        base_env: Mapping[str, str] | None = None,
        docker_exe: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        compose_up_timeout_s: float = COMPOSE_UP_TIMEOUT_S,
        clock: Callable[[], datetime] | None = None,
        project: str = DEFAULT_COMPOSE_PROJECT,
    ) -> None:
        self._project = validate_compose_project(project)
        self._run = run
        self._base_env = base_env
        self._docker_exe = docker_exe if docker_exe is not None else shutil.which("docker")
        self._timeout_s = timeout_s
        self._compose_up_timeout_s = compose_up_timeout_s
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def docker_exe(self) -> str | None:
        return self._docker_exe

    def _exe(self) -> str:
        if not self._docker_exe:
            raise DockerUnavailableError("docker executable not found on PATH")
        return self._docker_exe

    def _call(
        self, args: list[str], *, overrides: Mapping[str, str] | None = None, timeout: float | None = None
    ) -> Any:
        argv = [self._exe(), *args]
        base = self._base_env if self._base_env is not None else os.environ
        env = build_child_env(base, overrides or {})
        try:
            result = self._run(
                argv,
                env=env,
                capture_output=True,
                timeout=self._timeout_s if timeout is None else timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerTimeoutError(f"{' '.join(argv)} timed out after {exc.timeout} s") from exc
        except OSError as exc:
            raise DockerUnavailableError(f"cannot run {argv[0]!r}: {exc}") from exc
        if result.returncode != 0:
            raise DockerCommandError(argv, result.returncode, _as_text(result.stderr))
        return result

    @property
    def project(self) -> str:
        return self._project

    def _compose(self, *args: str) -> list[str]:
        return ["compose", "-p", self._project, *args]

    def compose_up(self, service: str, *, env: Mapping[str, str], no_deps: bool, recreate: bool) -> None:
        args = ["up", "-d"]
        if no_deps:
            args.append("--no-deps")
        if recreate:
            args.append("--force-recreate")
        args.append(service)
        self._call(self._compose(*args), overrides=env, timeout=self._compose_up_timeout_s)

    def container_id(self, service: str) -> str:
        result = self._call(self._compose("ps", "-a", "-q", service))
        text = _as_text(result.stdout).strip()
        try:
            return _validate_container_id(text)
        except ValueError as exc:
            raise DockerOutputError(f"docker compose ps returned an invalid container id: {text!r}") from exc

    def inspect(self, container_id: str) -> InspectResult:
        cid = _validate_container_id(container_id)
        result = self._call(["inspect", cid])
        inspected_at = self._clock().astimezone(timezone.utc)
        raw = _as_bytes(result.stdout)
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise DockerOutputError(f"docker inspect output is not valid JSON: {exc}") from exc
        return InspectResult(raw=raw, parsed=parsed, inspect_time=inspected_at)

    def kill(self, container_id: str, signal: str) -> None:
        cid = _validate_container_id(container_id)
        if not isinstance(signal, str) or _SIGNAL_RE.fullmatch(signal) is None:
            raise ValueError(f"invalid signal {signal!r}")
        self._call(["kill", "--signal", signal, cid])

    def available(self) -> bool:
        if not self._docker_exe:
            return False
        try:
            self._call(["version", "--format", "{{.Server.Version}}"])
        except DockerError:
            return False
        return True
