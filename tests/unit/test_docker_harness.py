"""Docker-free tests of the live-Docker test harness isolation (task 8.3 preparation).

Nothing here starts Docker or opens a socket: it exercises the pure helper
tests/docker_integration/_isolation.py and statically scans the seven live modules (AST, no import of them).
"""
from __future__ import annotations

import ast
import importlib
import re
import subprocess
from pathlib import Path

import pytest

iso = importlib.import_module("tests.docker_integration._isolation")

LIVE_DIR = Path(__file__).resolve().parents[1] / "docker_integration"
M05 = ("test_compose_isolation", "test_volume_persistence", "test_collector_readonly_rootfs")
M07 = ("test_ordinary_500", "test_experiment_a", "test_experiment_b", "test_experiment_c")
LIVE = M05 + M07
EXPECTED_TESTS = {
    "test_compose_isolation": 18,
    "test_volume_persistence": 8,
    "test_collector_readonly_rootfs": 7,
    "test_ordinary_500": 2,
    "test_experiment_a": 4,
    "test_experiment_b": 6,
    "test_experiment_c": 7,
}
MIN_ASSERTS = {
    "test_compose_isolation": 69,
    "test_volume_persistence": 29,
    "test_collector_readonly_rootfs": 29,
    "test_ordinary_500": 56,
    "test_experiment_a": 76,
    "test_experiment_b": 89,
    "test_experiment_c": 108,
}
MARKERS = {
    "test_compose_isolation": ("r.status==200", "134217728", "no-new-privileges", "ReadonlyRootfs"),
    "test_volume_persistence": ("10001", "storage_ok", "_compose(\"restart\"", "expected exactly one sample record"),
    "test_collector_readonly_rootfs": ("ReadonlyRootfs", "errno=30", "--read-only", "no-new-privileges:true"),
    "test_ordinary_500": ("== 500", "RestartCount", "MIN_NEW_SAMPLES", "seq not consecutive"),
    "test_experiment_a": ("\"exit_code\": 1", "OOMKilled", "os._exit(1)", "== 1"),
    "test_experiment_b": ("137", "sigkill", "CLOCK_TOLERANCE_MS", "OOMKilled"),
    "test_experiment_c": ("OOMKilled", "137", "134217728", "docker_oom_kill"),
}


def _src(name: str) -> str:
    return (LIVE_DIR / f"{name}.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- static checks (operate on source text)


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def code_strings(src: str) -> list[str]:
    """All string constants in code (docstrings excluded; comments are not in the AST)."""
    tree = ast.parse(src)
    skip = _docstring_ids(tree)
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


def protected_literals(src: str) -> list[str]:
    bad = []
    for s in code_strings(src):
        if any(v in s for v in iso.PROTECTED_VOLUMES) or s in iso.FORBIDDEN_PROJECTS or re.search(r"-p\s+coroner\b", s):
            bad.append(s)
    return bad


def destructive_commands(src: str) -> list[str]:
    tree = ast.parse(src)
    skip = _docstring_ids(tree)
    found = []
    for node in ast.walk(tree):
        parts: list[str] = []
        if isinstance(node, (ast.List, ast.Tuple)):
            parts = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        elif isinstance(node, ast.Call):
            parts = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            parts = [node.value]
        text = " ".join(parts)
        if re.search(r"\bdown\s+(?:-v|--volumes)\b|--volumes|\bprune\b|\bvolume\s+(?:rm|remove)\b", text):
            found.append(text)
        elif "down" in parts and "-v" in parts:  # argv-style: ["docker", "compose", "down", "-v"]
            found.append(text)
        elif "volume" in parts and ("rm" in parts or "remove" in parts):
            found.append(text)
    return found


def shell_true_calls(src: str) -> list[int]:
    return [
        kw.value.lineno
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
    ]


def _is_fixture(fn: ast.FunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "fixture" and isinstance(target.value, ast.Name) and target.value.id == "pytest":
            return True
    return False


def _is_gate_stmt(stmt: ast.stmt) -> bool:
    value = getattr(stmt, "value", None)
    return isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "gate"


def gate_problems(src: str) -> list[str]:
    """Root fixtures (no fixture parameters) must start with gate(); at least one gated root fixture must exist."""
    problems = []
    gated = 0
    for fn in [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and _is_fixture(n)]:
        if fn.args.args:
            continue  # depends on another fixture, which is itself gated
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # docstring
        if body and _is_gate_stmt(body[0]):
            gated += 1
        else:
            problems.append(fn.name)
    if gated == 0:
        problems.append("<no gated root fixture>")
    return problems


def runner_project_problems(src: str) -> list[str]:
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript):
            sub = node.targets[0]
            if isinstance(sub.slice, ast.Constant) and sub.slice.value == "CORONER_COMPOSE_PROJECT":
                if isinstance(node.value, ast.Name) and node.value.id == "PROJECT":
                    return []
                return ["CORONER_COMPOSE_PROJECT is not set from PROJECT"]
    return ["no CORONER_COMPOSE_PROJECT assignment"]


def project_constant_problems(src: str, var: str) -> list[str]:
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PROJECT" for t in node.targets):
            v = node.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "selected_project" and isinstance(v.args[0], ast.Name) and v.args[0].id == var:
                return []
            return ["PROJECT is not selected_project(" + var + ")"]
    return ["no PROJECT constant"]


def count_items(src: str) -> int:
    total = 0
    for fn in ast.parse(src).body:
        if isinstance(fn, ast.FunctionDef) and fn.name.startswith("test_"):
            n = 1
            for dec in fn.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "parametrize":
                    n *= len(dec.args[1].elts)
            total += n
    return total


# ---------------------------------------------------------------- helper behaviour


def test_selected_project_reads_env_and_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(iso.M05_VAR, raising=False)
    assert iso.selected_project(iso.M05_VAR) == ""
    monkeypatch.setenv(iso.M05_VAR, "coroner-t83-m05")
    assert iso.selected_project(iso.M05_VAR) == "coroner-t83-m05"
    assert iso.M05_VAR == "CORONER_TEST_M05_PROJECT" and iso.M07_VAR == "CORONER_TEST_PROJECT"


def test_evidence_volume_derivation() -> None:
    assert iso.evidence_volume("coroner-t83") == "coroner-t83_coroner_evidence"
    assert iso.evidence_volume("coroner-t83-m05") == "coroner-t83-m05_coroner_evidence"
    assert iso.evidence_volume("") == ""


@pytest.mark.parametrize("project", ["coroner", "coroner-m07", "coroner-spike"])
def test_forbidden_projects_rejected(project: str) -> None:
    with pytest.raises(ValueError, match="protected"):
        iso.validate_selected_project(iso.M07_VAR, project)


@pytest.mark.parametrize("project", ["Coroner-T83", "", "-lead", "has space", "a.b", "a/b", "a\\b", "-v"])
def test_invalid_or_missing_names_rejected(project: str) -> None:
    with pytest.raises(ValueError):
        iso.validate_selected_project(iso.M05_VAR, project)


@pytest.mark.parametrize("project", ["coroner-t83", "coroner-t83-m05", "coroner-repro"])
def test_valid_isolated_names_accepted(project: str) -> None:
    assert iso.validate_selected_project(iso.M07_VAR, project) == project
    assert iso.evidence_volume(project) not in iso.PROTECTED_VOLUMES


def test_protected_volume_set_and_forbidden_projects() -> None:
    assert set(iso.PROTECTED_VOLUMES) == {
        "coroner_coroner_evidence",
        "coroner-spike_coroner_spike_evidence",
        "coroner-m07_coroner_evidence",
    }
    assert set(iso.FORBIDDEN_PROJECTS) == {"coroner", "coroner-m07", "coroner-spike"}


@pytest.mark.parametrize("var", ["CORONER_TEST_M05_PROJECT", "CORONER_TEST_PROJECT"])
@pytest.mark.parametrize("value", [None, "", "coroner", "coroner-m07", "Bad Name"])
def test_gate_fails_before_any_subprocess(monkeypatch: pytest.MonkeyPatch, var: str, value: str | None) -> None:
    calls: list[object] = []

    def boom(*a: object, **k: object) -> None:
        calls.append(a)
        raise AssertionError("subprocess used before the gate")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    if value is None:
        monkeypatch.delenv(var, raising=False)
    else:
        monkeypatch.setenv(var, value)
    with pytest.raises(pytest.fail.Exception) as info:
        iso.gate(var)
    assert var in str(info.value) and "coroner-t83" in str(info.value)
    assert calls == []


def test_gate_returns_selected_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(iso.M07_VAR, "coroner-t83")
    assert iso.gate(iso.M07_VAR) == "coroner-t83"


def test_runner_env_sets_project_without_mutating_base() -> None:
    base = {"A": "1"}
    env = iso.runner_env(base, "coroner-t83")
    assert env == {"A": "1", "CORONER_COMPOSE_PROJECT": "coroner-t83"} and base == {"A": "1"}


# ---------------------------------------------------------------- static scan of the live modules


@pytest.mark.parametrize("name", LIVE)
def test_live_module_static_safety(name: str) -> None:
    src = _src(name)
    assert protected_literals(src) == []
    assert destructive_commands(src) == []
    assert shell_true_calls(src) == []
    assert gate_problems(src) == []
    assert "tests.docker_integration._isolation" in src


@pytest.mark.parametrize("name", M05)
def test_m05_modules_select_m05_variable(name: str) -> None:
    assert project_constant_problems(_src(name), "M05_VAR") == []
    assert "gate(M05_VAR)" in _src(name)


@pytest.mark.parametrize("name", M07)
def test_m07_modules_select_m07_variable(name: str) -> None:
    assert project_constant_problems(_src(name), "M07_VAR") == []
    assert "gate(M07_VAR)" in _src(name)


@pytest.mark.parametrize("name", ["test_experiment_a", "test_experiment_b", "test_experiment_c"])
def test_runner_gets_selected_project(name: str) -> None:
    assert runner_project_problems(_src(name)) == []


def test_experiment_a_exact_container_check_is_project_scoped() -> None:
    src = _src("test_experiment_a")
    assert 'names == {f"{PROJECT}-collector-1", f"{PROJECT}-demo_app-1"}' in src
    assert "com.docker.compose.project={PROJECT}" in src
    assert re.search(r'"ps", "-a", "--filter", label, "--format", "\{\{\.Names\}\}"', src)


@pytest.mark.parametrize("name", LIVE)
def test_existing_assertions_preserved(name: str) -> None:
    src = _src(name)
    tree = ast.parse(src)
    assert sum(isinstance(n, ast.Assert) for n in ast.walk(tree)) >= MIN_ASSERTS[name]
    assert count_items(src) == EXPECTED_TESTS[name]
    for marker in MARKERS[name]:
        assert marker.lower() in src.lower(), marker


def test_total_live_tests_is_52_plus_placeholder() -> None:
    assert sum(count_items(_src(n)) for n in LIVE) == 52
    assert count_items((LIVE_DIR / "test_placeholder.py").read_text(encoding="utf-8")) == 1


def test_helper_has_no_docker_or_side_effects() -> None:
    src = (LIVE_DIR / "_isolation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "subprocess" not in imported and "socket" not in imported
    assert shell_true_calls(src) == []


# ---------------------------------------------------------------- negative controls (mutated source text)


def test_negative_control_reintroduced_protected_literal() -> None:
    mutated = _src("test_experiment_c") + '\nOLD_VOLUME = "coroner_coroner_evidence"\n'
    assert protected_literals(mutated) != []
    assert protected_literals(_src("test_experiment_c").replace("PROJECT = selected_project(M07_VAR)", 'PROJECT = "coroner"')) != [] or (
        project_constant_problems(_src("test_experiment_c").replace("PROJECT = selected_project(M07_VAR)", 'PROJECT = "coroner-x"'), "M07_VAR") != []
    )


def test_negative_control_removed_gate() -> None:
    mutated = _src("test_experiment_b").replace("    gate(M07_VAR)\n", "", 1)
    assert gate_problems(mutated) != []


def test_negative_control_literal_runner_project_and_volume_removal() -> None:
    src = _src("test_experiment_a")
    assert runner_project_problems(src.replace('runner_env["CORONER_COMPOSE_PROJECT"] = PROJECT', 'runner_env["CORONER_COMPOSE_PROJECT"] = "coroner-m07"')) != []
    assert destructive_commands(src + '\nCMD = ["docker", "compose", "down", "-v"]\n') != []
    assert shell_true_calls(src + "\nsubprocess.run('x', shell=True)\n") != []
