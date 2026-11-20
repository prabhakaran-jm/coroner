"""Property 29: Runner child process environment and argv.

**Validates: Requirements 16.1, 16.2**

DockerCli is driven with a fake `run` (a subprocess.run replacement); no process starts.
The oracle below is written independently of `build_child_env`.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from datetime import timezone
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coroner.runner import docker_port
from coroner.runner.docker_port import (
    DockerCli,
    DockerCommandError,
    DockerOutputError,
    DockerTimeoutError,
    DockerUnavailableError,
    build_child_env,
)

CID = "ab12" * 16
EXE_UNIX = "/usr/bin/docker"
EXE_WIN = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
_INLINE_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_key = st.text(
    alphabet=st.characters(blacklist_characters="=\x00", blacklist_categories=("Cs",)),
    min_size=1,
    max_size=12,
)
_value = st.text(alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), max_size=20)
_compose_key = st.sampled_from(
    ["CORONER_RUN_ID", "CORONER_ENABLE_DESTRUCTIVE_ENDPOINTS", "CORONER_DEMO_MEM_LIMIT", "PATH", "SystemRoot"]
)
_env = st.dictionaries(st.one_of(_key, _compose_key), _value, max_size=8)
_exe = st.sampled_from([EXE_UNIX, EXE_WIN, "docker"])


class FakeRun:
    def __init__(self, stdout: bytes = b"", returncode: int = 0, stderr: bytes = b"", raises: Exception | None = None):
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.stdout, self.returncode, self.stderr, self.raises = stdout, returncode, stderr, raises

    def __call__(self, argv: Any, **kwargs: Any) -> Any:
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def _oracle_env(base: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for k in base:
        if k not in overrides:
            expected[k] = base[k]
    for k in overrides:
        expected[k] = overrides[k]
    return expected


def _check_call(argv: Any, kwargs: dict[str, Any], exe: str, expected_env: dict[str, str]) -> None:
    assert isinstance(argv, list) and argv and all(type(a) is str for a in argv)
    assert argv[0] == exe
    assert not kwargs.get("shell", False)
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    assert isinstance(kwargs["timeout"], (int, float)) and kwargs["timeout"] > 0
    assert kwargs["env"] == expected_env
    assert not any(_INLINE_ASSIGN.match(a) for a in argv[1:])


@settings(max_examples=100, deadline=None)
@given(base=_env, overrides=_env, exe=_exe, no_deps=st.booleans(), recreate=st.booleans())
def test_compose_up_env_and_argv(base, overrides, exe, no_deps, recreate):
    base_before, overrides_before = copy.deepcopy(base), copy.deepcopy(overrides)
    environ_before = dict(os.environ)
    fake = FakeRun()
    cli = DockerCli(run=fake, base_env=base, docker_exe=exe)
    cli.compose_up("demo_app", env=overrides, no_deps=no_deps, recreate=recreate)
    (argv, kwargs), = fake.calls
    _check_call(argv, kwargs, exe, _oracle_env(base, overrides))
    assert kwargs["env"] is not base and kwargs["env"] is not overrides
    assert base == base_before and overrides == overrides_before
    assert dict(os.environ) == environ_before
    assert argv[1:5] == ["compose", "-p", "coroner", "up"]
    assert ("--no-deps" in argv) is no_deps and ("--force-recreate" in argv) is recreate
    assert argv[-1] == "demo_app" and "-d" in argv


@settings(max_examples=100, deadline=None)
@given(base=_env, exe=_exe)
def test_other_calls_preserve_base_env(base, exe):
    base_before = copy.deepcopy(base)
    for stdout, action in (
        (CID.encode() + b"\n", lambda c: c.container_id("demo_app")),
        (b"[{}]", lambda c: c.inspect(CID)),
        (b"", lambda c: c.kill(CID, "SIGKILL")),
        (b"27.0", lambda c: c.available()),
    ):
        fake = FakeRun(stdout=stdout)
        action(DockerCli(run=fake, base_env=base, docker_exe=exe))
        (argv, kwargs), = fake.calls
        _check_call(argv, kwargs, exe, _oracle_env(base, {}))
    assert base == base_before


@settings(max_examples=100, deadline=None)
@given(base=_env, overrides=_env)
def test_build_child_env_matches_oracle(base, overrides):
    before = copy.deepcopy(base), copy.deepcopy(overrides)
    result = build_child_env(base, overrides)
    assert result == _oracle_env(base, overrides)
    assert (base, overrides) == before


def test_default_base_env_is_os_environ_at_call_time_and_not_mutated(monkeypatch):
    monkeypatch.setenv("PATH", r"C:\Windows\system32;C:\Program Files\Docker\Docker\resources\bin")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("DOCKER_HOST", "npipe:////./pipe/docker_engine")
    fake = FakeRun()
    cli = DockerCli(run=fake, docker_exe=EXE_WIN)
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")  # set after construction
    before = dict(os.environ)
    cli.compose_up("demo_app", env={"CORONER_RUN_ID": "a-1", "PATH": "override"}, no_deps=True, recreate=True)
    env = fake.calls[0][1]["env"]
    # os.environ upper-cases keys on Windows, so compare to the snapshot rather than a spelling.
    assert env["SystemRoot" if "SystemRoot" in before else "SYSTEMROOT"] == r"C:\Windows"
    assert env["DOCKER_HOST"] == "npipe:////./pipe/docker_engine"
    assert env["DOCKER_CONTEXT"] == "desktop-linux"
    assert env["CORONER_RUN_ID"] == "a-1" and env["PATH"] == "override"
    assert dict(os.environ) == before
    assert "CORONER_RUN_ID" not in os.environ


def test_exact_argv_shapes():
    fake = FakeRun(stdout=(CID + "\r\n").encode())
    cli = DockerCli(run=fake, base_env={}, docker_exe=EXE_WIN)
    cli.compose_up("demo_app", env={}, no_deps=True, recreate=True)
    assert cli.container_id("demo_app") == CID
    fake.stdout = b"[{}]"
    cli.inspect(CID)
    cli.kill(CID, "SIGKILL")
    assert [c[0] for c in fake.calls] == [
        [EXE_WIN, "compose", "-p", "coroner", "up", "-d", "--no-deps", "--force-recreate", "demo_app"],
        [EXE_WIN, "compose", "-p", "coroner", "ps", "-a", "-q", "demo_app"],
        [EXE_WIN, "inspect", CID],
        [EXE_WIN, "kill", "--signal", "SIGKILL", CID],
    ]


def test_compose_up_flags_off():
    fake = FakeRun()
    DockerCli(run=fake, base_env={}, docker_exe="docker").compose_up("demo_app", env={}, no_deps=False, recreate=False)
    assert fake.calls[0][0] == ["docker", "compose", "-p", "coroner", "up", "-d", "demo_app"]


def test_docker_exe_resolved_with_shutil_which(monkeypatch):
    seen: list[str] = []

    def fake_which(name: str) -> str:
        seen.append(name)
        return EXE_WIN

    monkeypatch.setattr(docker_port.shutil, "which", fake_which)
    fake = FakeRun()
    cli = DockerCli(run=fake, base_env={})
    cli.kill(CID, "SIGKILL")
    assert seen == ["docker"] and cli.docker_exe == EXE_WIN and fake.calls[0][0][0] == EXE_WIN


def test_missing_docker_executable_is_typed_error_and_unavailable(monkeypatch):
    monkeypatch.setattr(docker_port.shutil, "which", lambda name: None)
    fake = FakeRun()
    cli = DockerCli(run=fake, base_env={})
    assert cli.available() is False
    with pytest.raises(DockerUnavailableError):
        cli.compose_up("demo_app", env={}, no_deps=True, recreate=True)
    with pytest.raises(DockerUnavailableError):
        cli.container_id("demo_app")
    assert fake.calls == []


def test_oserror_from_run_is_unavailable():
    cli = DockerCli(run=FakeRun(raises=FileNotFoundError("docker")), base_env={}, docker_exe="docker")
    assert cli.available() is False
    with pytest.raises(DockerUnavailableError):
        cli.container_id("demo_app")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "AB12" * 16,
        "ab12" * 15,
        "ab12" * 17,
        "g" * 64,
        CID + "\n",
        " " + CID,
        "--all",
        "..\\" + "a" * 61,
        None,
        123,
    ],
)
def test_invalid_container_ids_rejected_before_any_call(bad):
    fake = FakeRun(stdout=b"[{}]")
    cli = DockerCli(run=fake, base_env={}, docker_exe="docker")
    with pytest.raises(ValueError):
        cli.inspect(bad)
    with pytest.raises(ValueError):
        cli.kill(bad, "SIGKILL")
    assert fake.calls == []


@pytest.mark.parametrize("sig", ["", "-9 --x", "SIG KILL", "SIGKILL;rm", None, "A" * 17])
def test_invalid_signal_rejected(sig):
    fake = FakeRun()
    with pytest.raises(ValueError):
        DockerCli(run=fake, base_env={}, docker_exe="docker").kill(CID, sig)
    assert fake.calls == []


@pytest.mark.parametrize(
    "stdout",
    [b"", b"\n", b"abc\n", (CID + "\n" + CID + "\n").encode(), CID.upper().encode(), (CID + "x").encode()],
)
def test_container_id_output_must_be_single_64_hex(stdout):
    cli = DockerCli(run=FakeRun(stdout=stdout), base_env={}, docker_exe="docker")
    with pytest.raises(DockerOutputError):
        cli.container_id("demo_app")


def test_failed_subprocess_raises_command_error_with_details():
    fake = FakeRun(returncode=1, stderr=b"no such container")
    cli = DockerCli(run=fake, base_env={}, docker_exe="docker")
    with pytest.raises(DockerCommandError) as info:
        cli.kill(CID, "SIGKILL")
    assert info.value.returncode == 1 and "no such container" in info.value.stderr
    assert info.value.argv[0] == "docker"
    assert cli.available() is False  # non-zero `version` means the daemon is not usable


def test_timeout_is_typed_error():
    exc = subprocess.TimeoutExpired(["docker"], 5)
    cli = DockerCli(run=FakeRun(raises=exc), base_env={}, docker_exe="docker")
    with pytest.raises(DockerTimeoutError):
        cli.compose_up("demo_app", env={}, no_deps=True, recreate=True)
    assert cli.available() is False


def test_timeouts_are_explicit_and_compose_up_uses_longer_one():
    fake = FakeRun(stdout=CID.encode())
    cli = DockerCli(run=fake, base_env={}, docker_exe="docker", timeout_s=7, compose_up_timeout_s=99)
    cli.compose_up("demo_app", env={}, no_deps=True, recreate=True)
    cli.container_id("demo_app")
    assert [c[1]["timeout"] for c in fake.calls] == [99, 7]


def test_inspect_keeps_raw_bytes_parsed_json_and_utc_time():
    from datetime import datetime, timedelta

    raw = b'[{"Id": "' + CID.encode() + b'",\r\n "State": {"OOMKilled": true, "ExitCode": 137}}]\n'
    tz = timezone(timedelta(hours=5, minutes=30))
    cli = DockerCli(
        run=FakeRun(stdout=raw),
        base_env={},
        docker_exe="docker",
        clock=lambda: datetime(2024, 5, 1, 12, 0, 0, tzinfo=tz),
    )
    result = cli.inspect(CID)
    assert result.raw == raw
    assert result.parsed == json.loads(raw)
    assert result.inspect_time.utcoffset() == timedelta(0)
    assert result.inspect_time == datetime(2024, 5, 1, 6, 30, 0, tzinfo=timezone.utc)


def test_inspect_invalid_json_is_output_error():
    cli = DockerCli(run=FakeRun(stdout=b"not json"), base_env={}, docker_exe="docker")
    with pytest.raises(DockerOutputError):
        cli.inspect(CID)


def test_available_true_when_version_succeeds():
    fake = FakeRun(stdout=b"27.0.1")
    assert DockerCli(run=fake, base_env={}, docker_exe="docker").available() is True
    assert fake.calls[0][0][:2] == ["docker", "version"]


# ---------------------------------------------------------------------------------------------- compose project (M0.7)

_PROJECT_ORACLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}\Z")
_valid_project = st.from_regex(r"[a-z0-9][a-z0-9_-]{0,62}", fullmatch=True)
_INVALID_PROJECTS = [
    "", " ", "a b", " coroner", "coroner ", "Coroner", "COroner", "-x", "_x", "a.b", "a/b", "a\\b", "\u00e9", "a\u00e9",
    "coroner\n", "a" * 64, "a:b", "a;b", "a$b", "..", None, 5, b"coroner", ["coroner"],
]


def _compose_argvs(project: str | None, no_deps: bool, recreate: bool) -> tuple[DockerCli, FakeRun]:
    fake = FakeRun(stdout=CID.encode())
    kwargs: dict[str, Any] = {} if project is None else {"project": project}
    cli = DockerCli(run=fake, base_env={}, docker_exe="docker", **kwargs)
    cli.compose_up("demo_app", env={}, no_deps=no_deps, recreate=recreate)
    cli.container_id("demo_app")
    return cli, fake


def test_default_project_is_coroner():
    assert docker_port.COMPOSE_PROJECT == "coroner" == docker_port.DEFAULT_COMPOSE_PROJECT
    cli, fake = _compose_argvs(None, True, True)
    assert cli.project == "coroner"
    assert all(c[0][1:4] == ["compose", "-p", "coroner"] for c in fake.calls)


@pytest.mark.parametrize("no_deps", [False, True])
@pytest.mark.parametrize("recreate", [False, True])
def test_explicit_project_is_used_by_every_compose_argv(no_deps, recreate):
    cli, fake = _compose_argvs("coroner-m07", no_deps, recreate)
    assert cli.project == "coroner-m07"
    assert len(fake.calls) == 2
    for argv, _ in fake.calls:
        assert argv[1:4] == ["compose", "-p", "coroner-m07"]
        assert argv.count("-p") == 1 and "coroner" not in argv
    expected = ["docker", "compose", "-p", "coroner-m07", "up", "-d"]
    expected += (["--no-deps"] if no_deps else []) + (["--force-recreate"] if recreate else []) + ["demo_app"]
    assert fake.calls[0][0] == expected
    assert fake.calls[1][0] == ["docker", "compose", "-p", "coroner-m07", "ps", "-a", "-q", "demo_app"]


@settings(max_examples=100, deadline=None)
@given(project=_valid_project, no_deps=st.booleans(), recreate=st.booleans())
def test_any_valid_project_reaches_every_compose_argv(project, no_deps, recreate):
    assert _PROJECT_ORACLE.match(project)
    assert docker_port.validate_compose_project(project) == project
    cli, fake = _compose_argvs(project, no_deps, recreate)
    assert cli.project == project
    for argv, kwargs in fake.calls:
        assert argv[1:4] == ["compose", "-p", project]
        assert not kwargs.get("shell", False)


@settings(max_examples=200, deadline=None)
@given(name=st.text(max_size=70))
def test_validation_agrees_with_independent_oracle(name):
    expected_ok = _PROJECT_ORACLE.match(name) is not None
    fake = FakeRun()
    if expected_ok:
        assert DockerCli(run=fake, base_env={}, docker_exe="docker", project=name).project == name
    else:
        with pytest.raises(ValueError):
            DockerCli(run=fake, base_env={}, docker_exe="docker", project=name)
    assert fake.calls == []


@pytest.mark.parametrize("bad", _INVALID_PROJECTS, ids=[repr(b) for b in _INVALID_PROJECTS])
def test_invalid_project_rejected_before_any_run_call(bad):
    fake = FakeRun(stdout=CID.encode())
    with pytest.raises(ValueError, match="project"):
        DockerCli(run=fake, base_env={}, docker_exe="docker", project=bad)
    with pytest.raises(ValueError):
        docker_port.validate_compose_project(bad)
    assert fake.calls == []


def test_project_boundaries():
    for ok in ("a", "0", "a-", "a_", "coroner_m07", "9lives", "a" * 63):
        assert docker_port.validate_compose_project(ok) == ok


def test_project_keeps_env_inspect_kill_and_exe_behavior(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "npipe:////./pipe/docker_engine")
    monkeypatch.setenv("PATH", r"C:\Windows\system32")
    before = dict(os.environ)
    fake = FakeRun(stdout=CID.encode())
    cli = DockerCli(run=fake, docker_exe=EXE_WIN, project="coroner-m07")
    cli.compose_up("demo_app", env={"CORONER_RUN_ID": "r-1"}, no_deps=True, recreate=True)
    env = fake.calls[0][1]["env"]
    assert env["DOCKER_HOST"] == "npipe:////./pipe/docker_engine" and env["PATH"] == r"C:\Windows\system32"
    assert env["CORONER_RUN_ID"] == "r-1"
    assert dict(os.environ) == before and "CORONER_COMPOSE_PROJECT" not in os.environ
    fake.stdout = b"[{}]"
    cli.inspect(CID)
    cli.kill(CID, "SIGKILL")
    assert fake.calls[0][0][0] == EXE_WIN
    assert fake.calls[0][0][1:5] == ["compose", "-p", "coroner-m07", "up"]
    assert fake.calls[1][0] == [EXE_WIN, "inspect", CID]
    assert fake.calls[2][0] == [EXE_WIN, "kill", "--signal", "SIGKILL", CID]
    for argv, kwargs in fake.calls:
        assert not kwargs.get("shell", False)
        assert not any(_INLINE_ASSIGN.match(a) for a in argv[1:])
    for bad in (CID[:12], CID.upper(), CID + "0"):
        with pytest.raises(ValueError):
            cli.inspect(bad)
        with pytest.raises(ValueError):
            cli.kill(bad, "SIGKILL")
    assert len(fake.calls) == 3


# ----------------------------------------------------------------------------------- negative controls (in-memory mutants)


def project_problems(cli_cls: Any) -> list[str]:
    """Behavior check used on the real class (must be empty) and on mutants (must be non-empty)."""
    problems: list[str] = []
    fake = FakeRun(stdout=CID.encode())
    try:
        cli = cli_cls(run=fake, base_env={}, docker_exe="docker", project="coroner-m07")
        cli.compose_up("demo_app", env={}, no_deps=True, recreate=True)
        cli.container_id("demo_app")
    except Exception as exc:  # noqa: BLE001
        return [f"valid project failed: {exc!r}"]
    for argv, _ in fake.calls:
        if argv[1:4] != ["compose", "-p", "coroner-m07"]:
            problems.append(f"wrong project argv {argv}")
    for bad in ("", "Coroner", "-x", "a b", None):
        fake_bad = FakeRun()
        try:
            cli_cls(run=fake_bad, base_env={}, docker_exe="docker", project=bad)
            problems.append(f"invalid project {bad!r} accepted")
        except ValueError:
            pass
        if fake_bad.calls:
            problems.append("run called for invalid project")
    return problems


class _HardcodedProject(DockerCli):
    def _compose(self, *args: str) -> list[str]:
        return ["compose", "-p", "coroner", *args]


def test_project_problems_is_clean_for_the_real_class():
    assert project_problems(DockerCli) == []


def test_negative_control_hardcoded_coroner_project_is_caught():
    problems = project_problems(_HardcodedProject)
    assert problems and all("wrong project argv" in p for p in problems)


def test_negative_control_silent_acceptance_or_fallback_is_caught(monkeypatch):
    def lenient(name: object) -> str:
        return name if isinstance(name, str) and name else "coroner"  # accepts anything, falls back to coroner

    monkeypatch.setattr(docker_port, "validate_compose_project", lenient)
    problems = project_problems(DockerCli)
    assert any("accepted" in p for p in problems)
