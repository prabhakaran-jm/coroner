"""Static scan of the whole coroner/runner package for non-portable constructs.

**Validates: Requirements 16.1, 16.2, 16.3**

The scanner works on source text, so it covers every current and future module under
coroner/runner. Negative controls use in-memory sources only.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

RUNNER_DIR = Path(__file__).resolve().parents[3] / "coroner" / "runner"

FORBIDDEN_IMPORTS = frozenset({"fcntl", "pty", "termios", "resource", "grp", "pwd"})
FORBIDDEN_PROGRAMS = frozenset({"bash", "sh", "/bin/sh", "/bin/bash", "zsh", "sleep", "grep", "cat", "wc"})
FORBIDDEN_OS_CALLS = frozenset({"system", "popen", "putenv", "unsetenv", "fork", "execv", "execvp", "execl", "execlp"})
FORBIDDEN_SUBPROCESS_CALLS = frozenset({"getoutput", "getstatusoutput"})
ENVIRON_MUTATORS = frozenset({"update", "setdefault", "pop", "popitem", "clear"})
_INLINE_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def scan_source(source: str, name: str = "<memory>") -> list[str]:
    """Return a list of 'name:line: problem' strings for forbidden constructs in `source`."""
    try:
        tree = ast.parse(source, filename=name)
    except SyntaxError as exc:
        return [f"{name}:{exc.lineno}: syntax error: {exc.msg}"]
    docstrings = _docstring_nodes(tree)
    found: list[str] = []

    def hit(node: ast.AST, message: str) -> None:
        found.append(f"{name}:{getattr(node, 'lineno', 0)}: {message}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    hit(node, f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
                hit(node, f"from {node.module} import ...")
            if node.module == "os" and any(a.name in FORBIDDEN_OS_CALLS for a in node.names):
                hit(node, "from os import forbidden call")
            if node.module == "os.path" or (node.module == "os" and any(a.name == "path" for a in node.names)):
                hit(node, "os.path (use pathlib)")
        elif isinstance(node, ast.Call):
            callee = _dotted(node.func)
            for kw in node.keywords:
                if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                    hit(node, "shell= argument that is not literally False")
            if callee.startswith("os.") and callee.split(".")[1] in FORBIDDEN_OS_CALLS:
                hit(node, f"{callee}()")
            if callee.startswith("os.spawn"):
                hit(node, f"{callee}()")
            if callee.startswith("subprocess.") and callee.split(".")[1] in FORBIDDEN_SUBPROCESS_CALLS:
                hit(node, f"{callee}() runs through a shell")
            if callee.startswith("os.path"):
                hit(node, f"{callee}() (use pathlib)")
            if callee.startswith("os.environ.") and callee.rsplit(".", 1)[1] in ENVIRON_MUTATORS:
                hit(node, f"{callee}() mutates os.environ")
            if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)) and node.args[0].elts:
                first = node.args[0].elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    if _INLINE_ASSIGN.match(first.value):
                        hit(node, f"argv starts with inline assignment {first.value!r}")
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and _dotted(target.value) == "os.environ":
                    hit(node, "os.environ item assignment or deletion")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if node.value.strip().lower() in FORBIDDEN_PROGRAMS:
                hit(node, f"POSIX tool name {node.value!r}")
            if re.search(r"\b(?:/bin/)?(?:ba)?sh\s+-c\b", node.value):
                hit(node, "shell -c command string")
    return found


def scan_package(directory: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_bytes().decode("utf-8")
        findings.extend(scan_source(text, path.relative_to(directory).as_posix()))
    return findings


# --- the real runner package -------------------------------------------------------------


def test_runner_package_has_no_forbidden_constructs():
    assert RUNNER_DIR.is_dir()
    assert (RUNNER_DIR / "docker_port.py").is_file()
    assert scan_package(RUNNER_DIR) == []


def test_runner_modules_use_only_utf8_lf_sources():
    for path in RUNNER_DIR.rglob("*.py"):
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), path
        assert b"\r\n" not in data, path


# --- negative controls (in-memory sources only) -------------------------------------------

BAD_SOURCES = {
    "shell_true": "import subprocess\nsubprocess.run('ls', shell=True)\n",
    "shell_variable": "import subprocess\nsubprocess.run(x, shell=flag)\n",
    "os_system": "import os\nos.system('docker ps')\n",
    "os_popen": "import os\nos.popen('docker ps')\n",
    "from_os_system": "from os import system\n",
    "bash_literal": "import subprocess\nsubprocess.run(['bash', '-c', 'x'])\n",
    "sh_c_string": "CMD = 'sh -c \"docker ps\"'\n",
    "sleep_tool": "ARGV = ['sleep', '1']\n",
    "grep_tool": "ARGV = ['grep', 'x']\n",
    "fcntl_import": "import fcntl\n",
    "fcntl_from": "from fcntl import flock\n",
    "getoutput": "import subprocess\nsubprocess.getoutput('docker ps')\n",
    "inline_assign": "import subprocess\nsubprocess.run(['A=1', 'docker', 'ps'])\n",
    "environ_set": "import os\nos.environ['A'] = '1'\n",
    "environ_update": "import os\nos.environ.update({'A': '1'})\n",
    "environ_del": "import os\ndel os.environ['A']\n",
    "putenv": "import os\nos.putenv('A', '1')\n",
    "os_path_join": "import os\np = os.path.join('a', 'b')\n",
    "from_os_path": "from os.path import join\n",
}


@pytest.mark.parametrize("name", sorted(BAD_SOURCES))
def test_scanner_flags_forbidden_source(name):
    assert scan_source(BAD_SOURCES[name], name), f"scanner missed {name}"


GOOD_SOURCE = '''"""Mentions bash, fcntl, os.system and shell=True only in this docstring."""
import os
import subprocess
import time
from pathlib import Path

def f(env):
    base = Path("runs") / "x"
    time.sleep(0)
    os.replace("a", "b")
    os.fsync(0)
    value = os.environ.get("PATH")
    return subprocess.run(["docker", "ps"], env={**os.environ, **env}, shell=False, check=False), base, value
'''


def test_scanner_accepts_legitimate_source():
    assert scan_source(GOOD_SOURCE, "good") == []


def test_scan_package_reports_bad_file_among_good_ones(tmp_path):
    (tmp_path / "ok.py").write_text(GOOD_SOURCE, encoding="utf-8")
    sub = tmp_path / "experiments"
    sub.mkdir()
    (sub / "bad.py").write_text(BAD_SOURCES["os_system"], encoding="utf-8")
    findings = scan_package(tmp_path)
    assert len(findings) == 1 and findings[0].startswith("experiments/bad.py:2:")


def test_scanner_reports_syntax_errors_instead_of_passing():
    assert scan_source("def (:\n", "broken")


def test_docker_port_has_no_hardcoded_compose_project():
    """The project must come from the selected value; only the default constant may spell `coroner`."""
    source = (RUNNER_DIR / "docker_port.py").read_text(encoding="utf-8")
    assert not re.search(r"""["']-p["']\s*,\s*["']coroner["']""", source)
    assert source.count('"coroner"') == 1 and 'COMPOSE_PROJECT = "coroner"' in source
    assert '"-p", self._project' in source
    assert "@staticmethod\n    def _compose" not in source
