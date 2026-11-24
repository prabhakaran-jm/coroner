"""Parity of the Viewer tolerance limit with the M0 constant (task 2.15).

This is the only Viewer test that imports ``coroner.runner.evaluate``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from coroner.runner.evaluate import CLOCK_TOLERANCE_MS
from coroner.viewer import clocks

VIEWER_DIR = Path(clocks.__file__).resolve().parent

FORBIDDEN_MODULES = ("coroner.runner.evaluate", "coroner.runner.experiments")


def test_viewer_tolerance_limit_equals_m0_constant() -> None:
    assert clocks.VIEWER_CLOCK_TOLERANCE_MAX_MS == CLOCK_TOLERANCE_MS
    assert CLOCK_TOLERANCE_MS == 250


def _is_forbidden(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in FORBIDDEN_MODULES)


def test_no_viewer_module_imports_m0_evaluate_or_experiments_or_names_the_constant() -> None:
    files = sorted(VIEWER_DIR.glob("*.py"))
    assert files, "no viewer modules found"
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden(alias.name):
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = ("." * node.level) + (node.module or "")
                if _is_forbidden(module):
                    offenders.append(f"{path.name}: from {module}")
                if module == "coroner.runner" and any(
                    a.name in ("evaluate", "experiments") for a in node.names
                ):
                    offenders.append(f"{path.name}: from coroner.runner import evaluate/experiments")
                if any(a.name == "CLOCK_TOLERANCE_MS" for a in node.names):
                    offenders.append(f"{path.name}: imports CLOCK_TOLERANCE_MS")
            elif isinstance(node, ast.Name) and node.id == "CLOCK_TOLERANCE_MS":
                offenders.append(f"{path.name}: names CLOCK_TOLERANCE_MS")
            elif isinstance(node, ast.Attribute) and node.attr == "CLOCK_TOLERANCE_MS":
                offenders.append(f"{path.name}: names CLOCK_TOLERANCE_MS")
    assert offenders == []
