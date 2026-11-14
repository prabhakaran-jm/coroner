"""Tests for the Docker opt-in rules in tests/conftest.py (Requirements 13.6, 13.9 to 13.11).

Each case runs pytest in a child process against a tiny fixture project that carries a
copy of tests/conftest.py, so the real suite is never affected. Docker availability is
simulated through the config._coroner_docker_probe seam and never touches Docker.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFTEST = REPO_ROOT / "tests" / "conftest.py"

FAKE_DOCKER_PLUGIN = '''
import os
import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    mode = os.environ.get("FAKE_DOCKER", "")
    log = os.environ.get("FAKE_DOCKER_LOG")

    def probe():
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("probe called\\n")
        return None if mode == "available" else "simulated: docker daemon not running"

    if mode in ("available", "unavailable"):
        config._coroner_docker_probe = probe
'''

PASSING_TEST = "def test_ok():\n    assert True\n"
MARKED_TEST = "import pytest\n\n@pytest.mark.docker_integration\ndef test_marked():\n    assert True\n"
UNMARKED_TEST = "def test_unmarked_but_in_integration_dir():\n    assert True\n"
FAILING_MARKED_TEST = "import pytest\n\n@pytest.mark.docker_integration\ndef test_fails():\n    assert False\n"


class Result:
    def __init__(self, code: int, output: str) -> None:
        self.code = code
        self.output = output
        summary = [line for line in output.splitlines() if re.search(r"\d+ (passed|failed|skipped|deselected)", line)]
        text = summary[-1] if summary else ""
        self.counts = {name: int(n) for n, name in re.findall(r"(\d+) (passed|failed|skipped|deselected)", text)}

    def count(self, name: str) -> int:
        return self.counts.get(name, 0)


def make_project(root: Path, *, failing_integration: bool = False) -> Path:
    proj = root / "proj"
    (proj / "unit_ok").mkdir(parents=True)
    (proj / "docker_integration").mkdir()
    (proj / "conftest.py").write_text(CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")
    (proj / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    docker_integration: opt-in Docker tests\n", encoding="utf-8"
    )
    (proj / "fake_docker.py").write_text(FAKE_DOCKER_PLUGIN, encoding="utf-8")
    (proj / "unit_ok" / "test_a.py").write_text(PASSING_TEST, encoding="utf-8")
    (proj / "docker_integration" / "test_marked.py").write_text(MARKED_TEST, encoding="utf-8")
    (proj / "docker_integration" / "test_unmarked.py").write_text(UNMARKED_TEST, encoding="utf-8")
    if failing_integration:
        (proj / "docker_integration" / "test_fails.py").write_text(FAILING_MARKED_TEST, encoding="utf-8")
    return proj


def run_pytest(proj: Path, *args: str, env_extra: dict[str, str] | None = None, use_fake: bool = True, quiet: bool = True) -> Result:
    env = {k: v for k, v in os.environ.items() if k not in {"CORONER_RUN_DOCKER", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "FAKE_DOCKER", "FAKE_DOCKER_LOG"}}
    env["PYTHONPATH"] = str(proj)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(env_extra or {})
    cmd = [sys.executable, "-m", "pytest", "-rs", "-p", "no:cacheprovider"] + (["-q"] if quiet else [])
    if use_fake:
        cmd += ["-p", "fake_docker"]
    proc = subprocess.run(cmd + list(args), cwd=proj, env=env, capture_output=True, text=True, timeout=120)
    return Result(proc.returncode, proc.stdout + proc.stderr)


def test_default_run_deselects_marked_and_directory_tests(tmp_path: Path) -> None:
    result = run_pytest(make_project(tmp_path), env_extra={"FAKE_DOCKER": "available"})
    assert result.code == 0, result.output
    assert result.count("passed") == 1
    assert result.count("deselected") == 2
    assert result.count("skipped") == 0


def test_header_states_how_to_opt_in(tmp_path: Path) -> None:
    result = run_pytest(make_project(tmp_path), env_extra={"FAKE_DOCKER": "available"}, quiet=False)
    assert result.code == 0, result.output
    assert "docker_integration tests are deselected (opt in with --run-docker or CORONER_RUN_DOCKER=1)" in result.output
    opted = run_pytest(make_project(tmp_path / "b"), "--run-docker", env_extra={"FAKE_DOCKER": "available"}, quiet=False)
    assert "docker_integration tests enabled (opted in)" in opted.output


def test_default_run_never_probes_docker(tmp_path: Path) -> None:
    log = tmp_path / "probe.log"
    result = run_pytest(make_project(tmp_path), env_extra={"FAKE_DOCKER": "available", "FAKE_DOCKER_LOG": str(log)})
    assert result.code == 0, result.output
    assert not log.exists()


def test_run_docker_flag_runs_integration_tests(tmp_path: Path) -> None:
    log = tmp_path / "probe.log"
    result = run_pytest(
        make_project(tmp_path), "--run-docker", env_extra={"FAKE_DOCKER": "available", "FAKE_DOCKER_LOG": str(log)}
    )
    assert result.code == 0, result.output
    assert result.count("passed") == 3
    assert result.count("deselected") == 0
    assert log.read_text(encoding="utf-8").count("probe called") == 1


def test_environment_variable_opt_in_runs_integration_tests(tmp_path: Path) -> None:
    result = run_pytest(make_project(tmp_path), env_extra={"CORONER_RUN_DOCKER": "1", "FAKE_DOCKER": "available"})
    assert result.code == 0, result.output
    assert result.count("passed") == 3
    assert result.count("deselected") == 0


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "2", " 1"])
def test_other_environment_values_do_not_opt_in(tmp_path: Path, value: str) -> None:
    result = run_pytest(make_project(tmp_path), env_extra={"CORONER_RUN_DOCKER": value, "FAKE_DOCKER": "available"})
    assert result.code == 0, result.output
    assert result.count("passed") == 1
    assert result.count("deselected") == 2


def test_opted_in_without_docker_skips_with_reason_and_still_runs_unit_tests(tmp_path: Path) -> None:
    result = run_pytest(make_project(tmp_path), "--run-docker", env_extra={"FAKE_DOCKER": "unavailable"})
    assert result.code == 0, result.output
    assert result.count("passed") == 1
    assert result.count("skipped") == 2
    assert result.count("deselected") == 0
    assert "Docker is unavailable: simulated: docker daemon not running" in result.output


def test_real_probe_reports_missing_docker_executable(tmp_path: Path) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    result = run_pytest(make_project(tmp_path), "--run-docker", env_extra={"PATH": str(empty_path)}, use_fake=False)
    assert result.code == 0, result.output
    assert result.count("skipped") == 2
    assert "Docker is unavailable: the docker executable was not found on PATH" in result.output


def test_failing_opted_in_integration_test_fails_the_run_but_not_unit_results(tmp_path: Path) -> None:
    proj = make_project(tmp_path, failing_integration=True)
    result = run_pytest(proj, "--run-docker", env_extra={"FAKE_DOCKER": "available"})
    assert result.code == 1, result.output
    assert result.count("failed") == 1
    assert result.count("passed") == 3


def test_default_run_ignores_a_failing_integration_test(tmp_path: Path) -> None:
    proj = make_project(tmp_path, failing_integration=True)
    result = run_pytest(proj, env_extra={"FAKE_DOCKER": "available"})
    assert result.code == 0, result.output
    assert result.count("failed") == 0
    assert result.count("deselected") == 3


def test_repository_registers_the_marker_and_the_option() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--markers", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "@pytest.mark.docker_integration" in proc.stdout
    helper = subprocess.run(
        [sys.executable, "-m", "pytest", "--help", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert "--run-docker" in helper.stdout