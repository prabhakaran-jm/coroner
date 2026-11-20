"""Probe static and size tests (Task 4.9; Requirements 3.2, 7.3, 14.4, 14.6; design D35, D36).

Each check is a small pure helper over a list of (relative path, source) pairs, so the same
code runs on the real package and on throw-away fixture packages (negative controls).

* size:    non-blank lines (comments and docstrings count) over every *.py under
           coroner/probe/ (recursive, includes __init__.py) is at most 300.
* stdlib:  AST import check. Allowed: top-level names in sys.stdlib_module_names, and
           coroner.probe[.x] (absolute or relative resolving inside coroner.probe). Any other
           coroner.* or third-party module fails. Constant `importlib.import_module("m")` and
           `__import__("m")` count as imports; a non-constant dynamic import also fails.
* random:  rejects importing the `random` module (any form, incl. alias), `numpy.random`,
           `from numpy import random`, and attribute use through a random alias. `secrets` and
           `os.urandom` are not the random module and are allowed.
* signal/atexit: rejects importing atexit or signal (any form), `atexit.register`,
           `signal.signal`, `signal.setitimer` (via alias), any `.add_signal_handler` attribute,
           `sys.exitfunc`, `weakref.finalize`, and any `__del__` method. No semantic analysis.
* drift:   real Probe samples (no thread, no network) validate against validate_sample.

Overlap with tests/unit/test_structure.py: its Probe stdlib-only rule overlaps check 2 and is
deliberately left in place; this module adds size, random, atexit/signal, dynamic imports and
the schema drift guard, which test_structure.py does not cover.
Comments, docstrings and string literals never trigger a check because everything is AST-based.
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coroner.evidence.schema import SAMPLE_OPTIONAL_FIELDS, SAMPLE_REQUIRED_FIELDS, validate_sample
from coroner.probe import Probe, ProbeConfig
from coroner.probe.probe import NO_CONTAINER_ID_REASON

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_DIR = REPO_ROOT / "coroner" / "probe"
DEMO_DIR = REPO_ROOT / "coroner" / "demo_app"
MAX_LINES = 300
THIRD_PARTY = ("fastapi", "starlette", "httpx", "requests", "uvicorn")
FORBIDDEN_COMPONENTS = ("collector", "demo_app", "runner", "evidence", "common")

Sources = list[tuple[str, str]]


# ---------------------------------------------------------------- helpers
def collect_sources(root: Path) -> Sources:
    """Every *.py under `root`, recursively, as (posix path relative to root, source)."""
    return [(p.relative_to(root).as_posix(), p.read_text(encoding="utf-8")) for p in sorted(root.rglob("*.py"))]


def line_counts(sources: Sources) -> dict[str, int]:
    return {name: sum(1 for line in text.splitlines() if line.strip() != "") for name, text in sources}


def check_size(sources: Sources, limit: int = MAX_LINES) -> list[str]:
    counts = line_counts(sources)
    total = sum(counts.values())
    return [f"total {total} > {limit}; per file: {counts}"] if total > limit else []


def _package_parts(name: str) -> tuple[str, ...]:
    return ("coroner", "probe", *Path(name).parts[:-1])


def imported_modules(name: str, source: str) -> tuple[list[tuple[str, tuple[str, ...], int]], list[str]]:
    """((absolute module, imported names, lineno) list, problems) for one module."""
    found: list[tuple[str, tuple[str, ...], int]] = []
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend((a.name, (), node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names = tuple(a.name for a in node.names)
            if node.level:
                base = _package_parts(name)
                if node.level - 1 > len(base) - 1:
                    problems.append(f"{name}:{node.lineno} relative import escapes coroner")
                    continue
                base = base[: len(base) - (node.level - 1)]
                module = ".".join((*base, *(node.module.split(".") if node.module else ())))
            else:
                module = node.module or ""
            found.append((module, names, node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            is_dyn = (isinstance(func, ast.Name) and func.id == "__import__") or (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
            if is_dyn:
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append((arg.value, (), node.lineno))
                else:
                    problems.append(f"{name}:{node.lineno} dynamic import with a non-constant name")
    return found, problems


def check_imports(sources: Sources) -> list[str]:
    allowed_top = set(sys.stdlib_module_names) | {"__future__"}
    problems: list[str] = []
    for name, text in sources:
        found, extra = imported_modules(name, text)
        problems.extend(extra)
        for module, names, lineno in found:
            where = f"{name}:{lineno}"
            top = module.split(".")[0]
            if top == "coroner":
                targets = [module] if module != "coroner" or not names else [f"coroner.{n}" for n in names]
                for target in targets:
                    if not (target == "coroner.probe" or target.startswith("coroner.probe.")):
                        problems.append(f"{where} imports {target!r}, outside coroner.probe")
            elif top not in allowed_top:
                problems.append(f"{where} imports non-stdlib module {module!r}")
    return problems


def all_imported_modules(sources: Sources) -> set[str]:
    return {m for name, text in sources for m, _, _ in imported_modules(name, text)[0]}


def _aliases(tree: ast.AST, module: str) -> set[str]:
    """Local names bound to `module` (or its dotted children) by `import`."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == module or a.name.startswith(module + "."):
                    out.add(a.asname if a.asname else a.name.split(".")[0])
    return out


def _from_names(tree: ast.AST, module: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module == module:
            out.update(a.asname or a.name for a in node.names)
    return out


def check_no_random(sources: Sources) -> list[str]:
    problems: list[str] = []
    for name, text in sources:
        tree = ast.parse(text)
        found, _ = imported_modules(name, text)
        for module, names, lineno in found:
            parts = module.split(".")
            hit = parts[0] == "random" or module.startswith("numpy.random") or (
                module == "numpy" and "random" in names
            )
            if hit:
                problems.append(f"{name}:{lineno} uses the random module ({module!r} {names})")
        bound = _aliases(tree, "random") | _aliases(tree, "numpy.random")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bound:
                problems.append(f"{name}:{node.lineno} attribute use of random alias {node.value.id!r}")
    return problems


def check_no_atexit_signal(sources: Sources) -> list[str]:
    problems: list[str] = []
    for name, text in sources:
        tree = ast.parse(text)
        found, _ = imported_modules(name, text)
        for module, _names, lineno in found:
            if module.split(".")[0] in ("atexit", "signal"):
                problems.append(f"{name}:{lineno} imports {module!r}")
        mods = _aliases(tree, "atexit") | _aliases(tree, "signal") | _aliases(tree, "weakref")
        funcs = _from_names(tree, "atexit") | _from_names(tree, "signal")
        for node in ast.walk(tree):
            where = f"{name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Attribute):
                if node.attr == "add_signal_handler":
                    problems.append(f"{where} add_signal_handler")
                elif node.attr == "exitfunc" and isinstance(node.value, ast.Name) and node.value.id == "sys":
                    problems.append(f"{where} sys.exitfunc")
                elif isinstance(node.value, ast.Name) and node.value.id in mods and node.attr in (
                    "register", "signal", "setitimer", "finalize", "alarm", "set_wakeup_fd",
                ):
                    problems.append(f"{where} {node.value.id}.{node.attr}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in funcs:
                problems.append(f"{where} call to imported atexit/signal function {node.func.id!r}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__del__":
                problems.append(f"{where} defines __del__")
    return problems


CHECKS = {
    "size": check_size,
    "imports": check_imports,
    "random": check_no_random,
    "signal": check_no_atexit_signal,
}


def run_all(sources: Sources) -> dict[str, list[str]]:
    return {key: fn(sources) for key, fn in CHECKS.items()}


def build_package(tmp_path: Path, files: dict[str, str]) -> Sources:
    root = tmp_path / "coroner" / "probe"
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return collect_sources(root)


def _lines(n: int, prefix: str = "x") -> str:
    """n non-blank lines (first a comment, second a docstring line) with blank lines mixed in."""
    body = ["# comment", '"""docstring"""'] + [f"{prefix}{i} = {i}" for i in range(n - 2)]
    return "\n\n".join(body[i] for i in range(n)) + "\n"


# ---------------------------------------------------------------- real package
def test_real_probe_size_at_most_300_non_blank_lines() -> None:
    sources = collect_sources(PROBE_DIR)
    names = [n for n, _ in sources]
    assert "__init__.py" in names and "probe.py" in names
    counts = line_counts(sources)
    total = sum(counts.values())
    print(f"probe files={names} counts={counts} total={total}")
    assert total <= MAX_LINES, f"total {total}; per file: {counts}"


def test_real_probe_imports_stdlib_only() -> None:
    sources = collect_sources(PROBE_DIR)
    assert check_imports(sources) == []
    modules = all_imported_modules(sources)
    print(f"probe imports={sorted(modules)}")
    tops = {m.split(".")[0] for m in modules}
    assert not tops & set(THIRD_PARTY)
    assert tops - {"coroner"} <= set(sys.stdlib_module_names) | {"__future__"}
    assert "coroner.evidence" not in modules and not any(m.startswith("coroner.evidence") for m in modules)
    for comp in FORBIDDEN_COMPONENTS:
        assert not any(m == f"coroner.{comp}" or m.startswith(f"coroner.{comp}.") for m in modules)


def test_real_probe_has_no_random() -> None:
    assert check_no_random(collect_sources(PROBE_DIR)) == []


def test_real_demo_app_has_no_random() -> None:
    if not DEMO_DIR.is_dir():
        pytest.skip("demo_app source not present")
    sources = collect_sources(DEMO_DIR)
    print(f"demo_app files={[n for n, _ in sources]}")
    assert check_no_random(sources) == []


def test_real_probe_has_no_atexit_signal_or_del() -> None:
    sources = collect_sources(PROBE_DIR)
    # The real docstring mentions both words; AST checking must not trip on it.
    assert any("atexit or signal hook" in text for _, text in sources)
    assert check_no_atexit_signal(sources) == []


# ---------------------------------------------------------------- schema drift guard
FIXED_TS = datetime(2026, 3, 4, 5, 6, 7, 891234, tzinfo=timezone.utc)
CONTAINER = "0123456789abcdef" * 4


def _probe(rss, container) -> Probe:
    return Probe(
        ProbeConfig(run_id="run-1", collector_url="http://collector:8081", interval_ms=500),
        rss_reader=rss,
        pid_reader=lambda: 4242,
        container_id_reader=container,
        wall_clock=lambda: FIXED_TS,
    )


def _good_sample() -> dict:
    probe = _probe(lambda: 123456789, lambda: (CONTAINER, "mountinfo"))
    for _ in range(3):
        probe.count_request()
    return probe.build_sample(2.5)


def test_drift_guard_full_sample() -> None:
    sample = _good_sample()
    assert validate_sample(sample) == []
    assert sample == {
        "run_id": "run-1", "seq": 0, "ts": "2026-03-04T05:06:07.891Z", "rss_bytes": 123456789,
        "request_count": 3, "loop_delay_ms": 2.5, "pid": 4242, "container_id": CONTAINER,
        "container_id_source": "mountinfo", "interval_ms": 500, "dropped": 0,
    }
    assert set(sample) == set(SAMPLE_REQUIRED_FIELDS)


def test_drift_guard_null_sample() -> None:
    def no_rss() -> int:
        raise OSError("no statm")

    probe = _probe(no_rss, lambda: (None, "none"))
    sample = probe.build_sample(None, "no delay measured yet")
    assert validate_sample(sample) == []
    assert sample["rss_bytes"] is None and sample["container_id"] is None
    assert sample["container_id_source"] == "none" and sample["loop_delay_ms"] is None
    assert sample["null_reasons"] == {
        "rss_bytes": "rss unavailable: OSError",
        "container_id": NO_CONTAINER_ID_REASON,
        "loop_delay_ms": "no delay measured yet",
    }
    assert set(sample) == set(SAMPLE_REQUIRED_FIELDS) | set(SAMPLE_OPTIONAL_FIELDS)


def test_drift_guard_detects_missing_required_field() -> None:
    for field in SAMPLE_REQUIRED_FIELDS:
        sample = _good_sample()
        del sample[field]
        assert validate_sample(sample) != [], field


def test_drift_guard_detects_unknown_field() -> None:
    sample = _good_sample()
    sample["surprise"] = 1
    assert validate_sample(sample) != []


# ---------------------------------------------------------------- negative controls
def test_size_exactly_300_passes_and_301_fails(tmp_path: Path) -> None:
    ok = build_package(tmp_path / "ok", {"__init__.py": _lines(100), "sub/m.py": _lines(200)})
    assert line_counts(ok) == {"__init__.py": 100, "sub/m.py": 200}
    assert check_size(ok) == []
    bad = build_package(tmp_path / "bad", {"__init__.py": _lines(101), "sub/m.py": _lines(200)})
    problems = check_size(bad)
    assert len(problems) == 1 and "total 301" in problems[0]
    assert "'__init__.py': 101" in problems[0] and "'sub/m.py': 200" in problems[0]


BAD_IMPORTS = {
    "requests": ("import requests\n", "requests"),
    "alias_httpx": ("import httpx as h\n", "httpx"),
    "from_fastapi_alias": ("from fastapi import FastAPI as F\n", "fastapi"),
    "from_evidence": ("from coroner.evidence import schema\n", "coroner.evidence"),
    "import_collector_alias": ("import coroner.collector.app as a\n", "coroner.collector.app"),
    "from_coroner_names": ("from coroner import evidence\n", "coroner.evidence"),
    "dynamic_import_module": ('import importlib\nimportlib.import_module("requests")\n', "requests"),
    "dynamic_dunder_import": ('__import__("uvicorn")\n', "uvicorn"),
    "dynamic_nonconstant": ("import importlib\nimportlib.import_module(name)\n", "non-constant"),
    "relative_escape": ("from .. import x\n", "coroner.x"),
}


@pytest.mark.parametrize("key", BAD_IMPORTS)
def test_import_check_rejects(key: str, tmp_path: Path) -> None:
    source, fragment = BAD_IMPORTS[key]
    problems = check_imports(build_package(tmp_path, {"m.py": source}))
    assert problems and fragment in " ".join(problems)


def test_import_check_accepts_relative_and_absolute_inside_probe(tmp_path: Path) -> None:
    files = {
        "__init__.py": "from . import m\nfrom .m import thing\nfrom __future__ import annotations\n",
        "m.py": "import os, json\nimport coroner.probe.sub.deep\nfrom coroner.probe import thing\nfrom .sub import deep\n",
        "sub/deep.py": "from .. import m\nfrom ..m import thing\nfrom . import other\n",
    }
    assert check_imports(build_package(tmp_path, files)) == []


BAD_RANDOM = {
    "import": "import random\n",
    "from": "from random import randint\n",
    "alias": "import random as rnd\n",
    "numpy_from": "from numpy import random\n",
    "numpy_dotted": "import numpy.random as nr\n",
    "dynamic": 'import importlib\nimportlib.import_module("random")\n',
}


@pytest.mark.parametrize("key", BAD_RANDOM)
def test_random_check_rejects(key: str, tmp_path: Path) -> None:
    assert check_no_random(build_package(tmp_path, {"m.py": BAD_RANDOM[key]}))


def test_random_check_allows_secrets_and_urandom(tmp_path: Path) -> None:
    src = "import os\nimport secrets\nsecrets.token_hex(4)\nos.urandom(2)\n"
    assert check_no_random(build_package(tmp_path, {"m.py": src})) == []


BAD_SIGNAL = {
    "atexit_register": "import atexit\natexit.register(f)\n",
    "atexit_from_alias": "from atexit import register as r\nr(f)\n",
    "signal_signal": "import signal\nsignal.signal(signal.SIGTERM, h)\n",
    "signal_alias_setitimer": "import signal as s\ns.setitimer(0, 1)\n",
    "add_signal_handler": "loop.add_signal_handler(2, f)\n",
    "exitfunc": "import sys\nsys.exitfunc = f\n",
    "weakref_finalize": "import weakref\nweakref.finalize(o, f)\n",
    "del_method": "class P:\n    def __del__(self):\n        pass\n",
}


@pytest.mark.parametrize("key", BAD_SIGNAL)
def test_signal_check_rejects(key: str, tmp_path: Path) -> None:
    assert check_no_atexit_signal(build_package(tmp_path, {"m.py": BAD_SIGNAL[key]}))


def test_words_in_docstrings_comments_and_strings_pass_all_checks(tmp_path: Path) -> None:
    src = '''"""random, atexit, signal; import random; signal.signal(1, f); import requests."""
# import random
# import atexit; atexit.register(f)
x = "import random"
y = 'from fastapi import FastAPI'
signal_ready = 1
random_value = 2


def f():
    """loop.add_signal_handler is not called here; no __del__ either."""
    return "weakref.finalize"
'''
    results = run_all(build_package(tmp_path, {"m.py": src}))
    assert results == {key: [] for key in CHECKS}
