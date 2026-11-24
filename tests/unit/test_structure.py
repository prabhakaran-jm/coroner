"""Structure tests (Requirements 14.4, 14.5, 13.8).

* AST import-graph rules from the design's dependency direction, plus tests that the
  checker really detects prohibited imports (so an empty package cannot pass vacuously).
* The Probe, and every package the stdlib-only host runner depends on, import only the
  standard library.
* Fresh-interpreter import of every Coroner package, using sys.executable only.
* Exact `==` pins in requirements/*.txt.

The tests read source files and run Python; they do not touch Docker or the network.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "coroner"
REQ_DIR = REPO_ROOT / "requirements"

PACKAGES = ("common", "evidence", "probe", "demo_app", "collector", "runner", "viewer")
IMPORTABLE = (
    "coroner",
    "coroner.common",
    "coroner.evidence",
    "coroner.probe",
    "coroner.demo_app",
    "coroner.collector",
    "coroner.runner",
    "coroner.runner.experiments",
    "coroner.viewer",
)

# Design, "Dependency direction": evidence -> common; collector -> evidence, common;
# runner -> evidence, common; demo_app -> probe; probe and common import no Coroner package.
# Nothing imports runner, collector, or demo_app except their own entry points.
ALLOWED_COMPONENTS: dict[str, frozenset[str]] = {
    "common": frozenset(),
    "evidence": frozenset({"common"}),
    "probe": frozenset(),
    "demo_app": frozenset({"probe"}),
    "collector": frozenset({"evidence", "common"}),
    "runner": frozenset({"evidence", "common"}),
    # The Viewer is read-only. The package-level rule allows runner; the finer module-level
    # rule (only runner.manifest) is RUNNER_MODULES_ALLOWED below and in
    # tests/unit/viewer/test_import_boundary.py.
    "viewer": frozenset({"evidence", "common", "runner"}),
}

# Finer rule inside an allowed component: only these coroner.runner modules may be imported.
RUNNER_MODULES_ALLOWED: dict[str, frozenset[str]] = {
    "viewer": frozenset({"coroner.runner", "coroner.runner.manifest"}),
}

# Third-party imports. The Probe is stdlib-only (design). The host runner is stdlib-only
# (requirements/runner.txt has no packages, task 1.5), so everything it imports, which is
# common and evidence, must be stdlib-only too. demo_app and collector may use only what
# their own requirements file pins.
STDLIB_ONLY = frozenset({"common", "evidence", "probe", "runner"})
PIN_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]]+==\S+$")
FORBIDDEN_VERSION_CHARS = frozenset("*<>~!,; ")


class ImportRef(NamedTuple):
    module: str
    lineno: int


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def imported_modules(source: str, package: tuple[str, ...]) -> list[ImportRef]:
    """Absolute module names imported by `source`, which lives in `package` (dotted parts)."""
    refs: list[ImportRef] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            refs.extend(ImportRef(alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                base = list(package[: max(keep, 0)])
                if node.module:
                    base.append(node.module)
                module = ".".join(base)
            else:
                module = node.module or ""
            refs.append(ImportRef(module, node.lineno))
            if module == "coroner":  # `from coroner import runner` imports a subpackage
                refs.extend(ImportRef(f"coroner.{alias.name}", node.lineno) for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in {"import_module", "__import__"} and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    refs.append(ImportRef(arg.value, node.lineno))
    return refs


def import_violations(
    own: str, refs: list[ImportRef], third_party: frozenset[str] = frozenset()
) -> list[str]:
    """Describe every import that breaks the dependency rules for coroner.<own>."""
    problems: list[str] = []
    allowed = ALLOWED_COMPONENTS[own]
    for ref in refs:
        parts = ref.module.split(".")
        top = parts[0]
        if top == "coroner":
            if len(parts) > 1 and parts[1] != own and parts[1] not in allowed:
                problems.append(f"line {ref.lineno}: coroner.{own} must not import {ref.module}")
            elif (
                own in RUNNER_MODULES_ALLOWED
                and len(parts) > 1
                and parts[1] == "runner"
                and ref.module not in RUNNER_MODULES_ALLOWED[own]
            ):
                problems.append(f"line {ref.lineno}: coroner.{own} must not import {ref.module}")
        elif top in sys.stdlib_module_names or top == "":
            continue
        elif _normalise(top) not in third_party:
            kind = "the standard library only" if not third_party else "its pinned requirements only"
            problems.append(f"line {ref.lineno}: coroner.{own} may import {kind}, found {ref.module}")
    return problems


def pinned_names(path: Path) -> frozenset[str]:
    return frozenset(_normalise(line.split("==")[0].split("[")[0]) for line in pin_lines(path))


def pin_lines(path: Path) -> list[str]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def third_party_for(own: str) -> frozenset[str]:
    if own in STDLIB_ONLY:
        return frozenset()
    if own == "viewer":  # no requirements/viewer.txt exists; the Viewer runs on the collector pins
        return pinned_names(REQ_DIR / "collector.txt")
    return pinned_names(REQ_DIR / f"{own}.txt")


def package_files(own: str) -> list[Path]:
    return sorted((SRC_ROOT / own).rglob("*.py"))


def package_parts(path: Path) -> tuple[str, ...]:
    return path.relative_to(REPO_ROOT).parent.parts


def pin_problem(line: str) -> str | None:
    if not PIN_RE.match(line):
        return "not an exact name==version pin"
    version = line.split("==", 1)[1]
    if not version or any(ch in version for ch in FORBIDDEN_VERSION_CHARS) or "=" in version:
        return "version contains a range, wildcard, marker, or comparison character"
    return None


# --- AST import rules on the real source tree -------------------------------------


@pytest.mark.parametrize("own", PACKAGES)
def test_package_follows_the_allowed_import_rules(own: str) -> None:
    files = package_files(own)
    assert files, f"coroner/{own} has no Python files; the skeleton is missing"
    third_party = third_party_for(own)
    problems: list[str] = []
    for path in files:
        refs = imported_modules(path.read_text(encoding="utf-8"), package_parts(path))
        problems += [f"{path.relative_to(REPO_ROOT)}: {p}" for p in import_violations(own, refs, third_party)]
    assert not problems, "\n".join(problems)


def test_probe_imports_only_the_standard_library() -> None:
    files = package_files("probe")
    assert files
    for path in files:
        for ref in imported_modules(path.read_text(encoding="utf-8"), package_parts(path)):
            parts = ref.module.split(".")
            is_probe_itself = parts[:2] == ["coroner", "probe"] or ref.module == "coroner"
            assert is_probe_itself or parts[0] in sys.stdlib_module_names, (
                f"{path.relative_to(REPO_ROOT)} line {ref.lineno} imports {ref.module}"
            )


# --- the checker itself must detect violations ------------------------------------


@pytest.mark.parametrize(
    "own, source, parts",
    [
        ("probe", "import coroner.common", ("coroner", "probe")),
        ("probe", "from coroner.evidence import schema", ("coroner", "probe")),
        ("probe", "import fastapi", ("coroner", "probe")),
        ("probe", "import yaml", ("coroner", "probe")),
        ("probe", "from coroner import demo_app", ("coroner", "probe")),
        ("probe", "from .. import runner", ("coroner", "probe")),
        ("probe", "from ..collector import app", ("coroner", "probe")),
        ("common", "from coroner.evidence import writer", ("coroner", "common")),
        ("evidence", "import coroner.collector", ("coroner", "evidence")),
        ("evidence", "import pydantic", ("coroner", "evidence")),
        ("demo_app", "from coroner.collector.app import app", ("coroner", "demo_app")),
        ("demo_app", "from coroner import runner", ("coroner", "demo_app")),
        ("demo_app", "from coroner.evidence import schema", ("coroner", "demo_app")),
        ("collector", "from coroner.demo_app import main", ("coroner", "collector")),
        ("collector", "from coroner.runner import cli", ("coroner", "collector")),
        ("collector", "import coroner.probe", ("coroner", "collector")),
        ("runner", "from coroner.collector import app", ("coroner", "runner", "experiments")),
        ("runner", "import coroner.demo_app", ("coroner", "runner")),
        ("runner", "import httpx", ("coroner", "runner")),
        ("runner", "from ...demo_app import main", ("coroner", "runner", "experiments")),
        ("probe", "import importlib\nimportlib.import_module('coroner.common')", ("coroner", "probe")),
        ("probe", "__import__('coroner.runner')", ("coroner", "probe")),
        ("probe", "def f():\n    import coroner.evidence", ("coroner", "probe")),
        ("viewer", "import coroner.collector", ("coroner", "viewer")),
        ("viewer", "from coroner.probe import x", ("coroner", "viewer")),
        ("viewer", "import docker", ("coroner", "viewer")),
        ("viewer", "import yaml", ("coroner", "viewer")),
        ("viewer", "from coroner.runner.experiments.base import parse_docker_time", ("coroner", "viewer")),
    ],
)
def test_checker_detects_prohibited_imports(own: str, source: str, parts: tuple[str, ...]) -> None:
    third_party = frozenset({"fastapi", "starlette", "pydantic", "uvicorn"}) if own in {"demo_app", "collector", "viewer"} else frozenset()
    assert import_violations(own, imported_modules(source, parts), third_party), source


@pytest.mark.parametrize(
    "own, source, parts",
    [
        ("probe", "from __future__ import annotations\nimport asyncio, os, json\nfrom urllib import request", ("coroner", "probe")),
        ("probe", "from . import sampler\nfrom .sender import Sender\nimport coroner.probe.sampler", ("coroner", "probe")),
        ("evidence", "from coroner.common import time\nfrom ..common import ids\nimport coroner", ("coroner", "evidence")),
        ("demo_app", "import fastapi\nfrom coroner.probe import start\nfrom starlette.requests import Request", ("coroner", "demo_app")),
        ("collector", "from coroner.evidence import writer\nfrom coroner.common import clock\nimport uvicorn", ("coroner", "collector")),
        ("runner", "from coroner.evidence import reader\nfrom coroner.common import clock\nimport subprocess, urllib.request", ("coroner", "runner")),
        ("runner", "from ..experiments import base\nfrom ...common import clock", ("coroner", "runner", "experiments")),
        ("viewer", "import fastapi", ("coroner", "viewer")),
        ("viewer", "from coroner.evidence import reader", ("coroner", "viewer")),
    ],
)
def test_checker_accepts_allowed_imports(own: str, source: str, parts: tuple[str, ...]) -> None:
    third_party = frozenset({"fastapi", "starlette", "pydantic", "uvicorn"}) if own in {"demo_app", "collector", "viewer"} else frozenset()
    assert import_violations(own, imported_modules(source, parts), third_party) == []


def test_relative_import_resolution() -> None:
    refs = imported_modules("from ... import common\nfrom ..x import y\nfrom . import z", ("coroner", "runner", "experiments"))
    modules = [r.module for r in refs]
    assert "coroner" in modules  # from ... import common
    assert "coroner.common" in modules  # alias of `from coroner import ...` is a sub-package
    assert "coroner.runner.x" in modules  # from ..x import y
    assert "coroner.runner.experiments" in modules  # from . import z


# --- fresh interpreter ------------------------------------------------------------


def _fresh_python(code: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("module", IMPORTABLE)
def test_package_imports_in_a_fresh_interpreter(module: str) -> None:
    proc = _fresh_python(f"import {module}")
    assert proc.returncode == 0, proc.stderr


def test_every_module_imports_in_one_fresh_interpreter() -> None:
    """Imports each module once, in dependency-agnostic order, to expose import cycles."""
    code = (
        "import importlib, pkgutil, coroner\n"
        "names = [m.name for m in pkgutil.walk_packages(coroner.__path__, 'coroner.')]\n"
        "for name in names:\n"
        "    importlib.import_module(name)\n"
        "print(len(names))\n"
    )
    proc = _fresh_python(code)
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout.strip()) >= len(IMPORTABLE) - 1  # every sub-package, not 'coroner' itself


def test_probe_import_loads_no_other_coroner_package_and_no_third_party_package() -> None:
    code = (
        "import sys, coroner.probe\n"
        "loaded = sorted(m for m in sys.modules if m == 'coroner' or m.startswith('coroner.'))\n"
        "third = sorted(m for m in ('fastapi', 'starlette', 'pydantic', 'uvicorn', 'httpx', 'yaml') if m in sys.modules)\n"
        "print(loaded)\nprint(third)\n"
    )
    proc = _fresh_python(code)
    assert proc.returncode == 0, proc.stderr
    loaded, third = (ast.literal_eval(line) for line in proc.stdout.strip().splitlines())
    assert all(m == "coroner" or m.startswith("coroner.probe") for m in loaded), loaded
    assert third == [], third


# --- exact pins -------------------------------------------------------------------

EXPECTED_FILES = ("collector.txt", "demo_app.txt", "dev.txt", "runner.txt")


def test_expected_requirements_files_exist() -> None:
    assert sorted(p.name for p in REQ_DIR.glob("*.txt")) == list(EXPECTED_FILES)


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_every_requirement_line_is_an_exact_pin(name: str) -> None:
    problems = [f"{line!r}: {why}" for line in pin_lines(REQ_DIR / name) if (why := pin_problem(line))]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_a_package_is_pinned_once_per_file(name: str) -> None:
    names = [_normalise(line.split("==")[0].split("[")[0]) for line in pin_lines(REQ_DIR / name)]
    assert len(names) == len(set(names))


def test_pins_agree_across_files_and_dev_is_a_superset() -> None:
    dev = {_normalise(l.split("==")[0]): l.split("==")[1] for l in pin_lines(REQ_DIR / "dev.txt")}
    for name in ("collector.txt", "demo_app.txt", "runner.txt"):
        for line in pin_lines(REQ_DIR / name):
            pkg, version = _normalise(line.split("==")[0]), line.split("==")[1]
            assert dev.get(pkg) == version, f"{name}: {line} is not pinned identically in dev.txt"


def test_runner_requirements_file_has_no_packages() -> None:
    assert pin_lines(REQ_DIR / "runner.txt") == []


@pytest.mark.parametrize(
    "line",
    ["fastapi>=0.115", "fastapi", "fastapi~=0.115.6", "fastapi==0.115.*", "fastapi==0.115.6,<1", "-r base.txt", "fastapi == 0.115.6", "fastapi==0.115.6 ; python_version>'3'", "git+https://example.com/x.git", "fastapi==", "./local"],
)
def test_pin_checker_rejects_non_exact_lines(line: str) -> None:
    assert pin_problem(line) is not None


@pytest.mark.parametrize("line", ["fastapi==0.115.6", "pydantic_core==2.50.4", "uvicorn[standard]==0.34.0", "typing-inspection==0.4.5", "PyYAML==6.0.2"])
def test_pin_checker_accepts_exact_pins(line: str) -> None:
    assert pin_problem(line) is None