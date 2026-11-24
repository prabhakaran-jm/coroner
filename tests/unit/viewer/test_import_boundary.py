"""Module-level import boundary of the Viewer (task 1.8).

Complements tests/unit/test_structure.py (package-level rules). Reads source
files only; it never starts a server and does not look at sys.modules.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_structure import REPO_ROOT, ImportRef, imported_modules

VIEWER_DIR = REPO_ROOT / "coroner" / "viewer"
PARTS = ("coroner", "viewer")

RUNNER_ALLOWED = frozenset({"coroner.runner", "coroner.runner.manifest"})
FORBIDDEN_TOP = frozenset({"subprocess", "socket", "urllib", "shutil", "ctypes", "multiprocessing"})
FORBIDDEN_DOTTED = ("http.client",)
UVICORN_FILES = frozenset({"cli.py"})
WEB_FRAMEWORK_FILES = frozenset({"app.py", "hostcheck.py"})
WEB_FRAMEWORKS = frozenset({"fastapi", "starlette"})


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _from_import_submodules(source: str) -> list[ImportRef]:
    """Submodules named by `from http import client` and `from coroner.runner import cli`.

    imported_modules reports only the package (`http`, `coroner.runner`).
    """
    refs: list[ImportRef] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in {"http", "coroner.runner"}:
            for alias in node.names:
                if node.module == "coroner.runner" or alias.name == "client":
                    refs.append(ImportRef(f"{node.module}.{alias.name}", node.lineno))
    return refs


def boundary_violations(filename: str, source: str) -> list[str]:
    """Describe every module-level boundary violation of one Viewer module."""
    refs = imported_modules(source, PARTS) + _from_import_submodules(source)
    problems: list[str] = []
    for ref in refs:
        module, top = ref.module, ref.module.split(".")[0]
        where = f"{filename} line {ref.lineno}: {module}"
        if _under(module, "coroner.runner") and module not in RUNNER_ALLOWED:
            problems.append(f"{where} (from runner only runner.manifest is allowed)")
        if _under(module, "coroner.runner.experiments.base"):
            problems.append(f"{where} (runner.experiments.base is forbidden)")
        if top == "uvicorn" and filename not in UVICORN_FILES:
            problems.append(f"{where} (uvicorn is allowed only in cli.py)")
        if top in WEB_FRAMEWORKS and filename not in WEB_FRAMEWORK_FILES:
            problems.append(f"{where} (fastapi/starlette only in app.py and hostcheck.py)")
        if top in FORBIDDEN_TOP or any(_under(module, d) for d in FORBIDDEN_DOTTED):
            problems.append(f"{where} (forbidden in the Viewer)")
    return problems


def _viewer_files() -> list[Path]:
    return sorted(VIEWER_DIR.rglob("*.py"))


def test_viewer_has_the_expected_modules() -> None:
    names = {p.name for p in _viewer_files()}
    assert {"app.py", "files.py", "cli.py", "hostcheck.py", "__init__.py", "__main__.py"} <= names


def test_viewer_modules_respect_the_module_level_boundary() -> None:
    problems: list[str] = []
    for path in _viewer_files():
        problems += boundary_violations(path.name, path.read_text(encoding="utf-8"))
    assert not problems, "\n".join(problems)


# --- the checker detects each violation on synthetic source -----------------------


@pytest.mark.parametrize(
    "filename, source",
    [
        ("app.py", "from coroner.runner.experiments import base"),
        ("app.py", "import coroner.runner.cli"),
        ("app.py", "from coroner.runner import cli"),
        ("app.py", "from coroner.runner.experiments.base import parse_docker_time"),
        ("app.py", "import coroner.runner.experiments.base"),
        ("app.py", "import uvicorn"),
        ("files.py", "from uvicorn import Config"),
        ("files.py", "import fastapi"),
        ("cli.py", "import starlette.requests"),
        ("files.py", "from starlette.responses import Response"),
        ("app.py", "import subprocess"),
        ("app.py", "import socket"),
        ("app.py", "import urllib.request"),
        ("app.py", "from urllib import parse"),
        ("app.py", "import http.client"),
        ("app.py", "from http import client"),
        ("app.py", "from http.client import HTTPConnection"),
        ("app.py", "import shutil"),
        ("app.py", "import ctypes"),
        ("app.py", "import multiprocessing"),
        ("app.py", "def f():\n    import subprocess"),
        ("app.py", "import importlib\nimportlib.import_module('shutil')"),
    ],
)
def test_checker_detects_each_violation(filename: str, source: str) -> None:
    assert boundary_violations(filename, source), source


@pytest.mark.parametrize(
    "filename, source",
    [
        ("app.py", "from coroner.runner.manifest import deserialize_manifest"),
        ("app.py", "from coroner.evidence.reader import read_evidence\nfrom coroner.common import ids"),
        ("app.py", "import fastapi\nfrom starlette.responses import Response"),
        ("hostcheck.py", "import json\nfrom starlette.requests import Request"),
        ("cli.py", "import uvicorn"),
        ("files.py", "import os, json\nfrom http import HTTPStatus"),
    ],
)
def test_checker_accepts_allowed_imports(filename: str, source: str) -> None:
    assert boundary_violations(filename, source) == []


# --- fresh interpreter ------------------------------------------------------------


def test_viewer_main_imports_in_a_fresh_interpreter() -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", "import coroner.viewer.__main__"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
