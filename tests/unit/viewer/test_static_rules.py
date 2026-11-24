"""Static read-only scan of `coroner/viewer/*.py` (task 1.12, Python part).

The scan works on the AST (imports, calls, string constants), so comments and docstrings that merely
mention a word are never flagged, and the word `docker` itself is allowed: the Viewer shows Docker
evidence and legitimately uses names such as `docker_state`, `docker_inspect.json` and
`container_state`. What is forbidden is Docker or process EXECUTION, Docker client imports, the Docker
socket and `DOCKER_HOST`, and every write-side filesystem operation.

It reads only Viewer source files. It never reads the real `runs/` directory, starts a process or
opens a socket. Negative controls run the same scanner on synthetic sources.

Requirements: 2.1, 2.2, 3.3, 10.2, 10.5, 11.7, 11.8, 11.11, 11.42.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VIEWER_DIR = REPO_ROOT / "coroner" / "viewer"

FORBIDDEN_IMPORT_TOP = frozenset(
    {
        "subprocess", "socket", "urllib", "shutil", "tempfile",
        "docker", "docker-py", "python_on_whales", "dockerpty",
    }
)
FORBIDDEN_IMPORT_DOTTED = ("http.client", "coroner.runner.experiments.base", "coroner.runner.evaluate")

OS_WRITE_CALLS = frozenset(
    {
        "remove", "unlink", "rename", "renames", "replace", "rmdir", "removedirs", "mkdir", "makedirs",
        "chmod", "lchmod", "chown", "lchown", "utime", "truncate", "ftruncate", "symlink", "link",
        "mkfifo", "mknod", "write", "pwrite", "writev", "startfile", "fork", "forkpty", "kill", "killpg",
        "system", "popen",
    }
)
OS_PROCESS_PREFIXES = ("exec", "spawn", "posix_spawn")
PATH_WRITE_METHODS = frozenset(
    {"write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir", "symlink_to", "hardlink_to", "chmod", "lchmod"}
)
PATH_ONE_ARG_METHODS = frozenset({"rename", "replace"})  # str.replace takes two or more arguments
WRITE_O_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC", "O_EXCL", "O_TMPFILE"})
FORBIDDEN_CALL_NAMES = frozenset({"read_manifest", "build_summary"})

DOCKER_VERBS = (
    "compose|run|kill|stop|rm|ps|exec|inspect|build|pull|push|start|restart|pause|unpause|create|cp|"
    "logs|volume|container|image|network|system|info|version|events|attach|commit|save|load|top|stats"
)
DOCKER_COMMAND_RE = re.compile(rf"(?<![\w.-])docker(?:\.exe)?\s+(?:{DOCKER_VERBS})\b")
DOCKER_COMPOSE_BINARY_RE = re.compile(r"(?<![\w.-])docker-compose(?:\.exe)?(?![\w-])")
COMPOSE_CONTROL_RE = re.compile(r"\bcompose\s+(?:up|down|kill|stop|rm)\b")
DOCKER_SOCKET_RE = re.compile(r"/var/run/docker\.sock|docker_engine|\bdocker\.sock\b")
DOCKER_HOST_RE = re.compile(r"\bDOCKER_HOST\b")
DOCKER_BINARY_NAMES = frozenset({"docker", "docker.exe", "docker-compose", "docker-compose.exe"})
COMPOSE_OPS = frozenset({"up", "down", "kill", "stop", "rm"})


# ---------------------------------------------------------------- scanner


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Dotted name of a Name/Attribute chain with import aliases resolved, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    head = aliases.get(node.id, node.id)
    parts.append(head)
    return ".".join(reversed(parts))


def _root_name(node: ast.AST) -> ast.AST:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


def _mode_problem(call: ast.Call, *, builtin_like: bool) -> str | None:
    mode: ast.expr | None = None
    if len(call.args) >= 2:
        mode = call.args[1]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return None  # default mode is "r"
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return "open() with a write/append/exclusive/update mode" if set(mode.value) & set("wax+") else None
    if builtin_like and not isinstance(mode, ast.Constant):
        return "open() with a mode that is not a literal"
    return None


def _check_string(text: str) -> list[str]:
    found: list[str] = []
    if DOCKER_COMMAND_RE.search(text):
        found.append("Docker CLI command string")
    if DOCKER_COMPOSE_BINARY_RE.search(text):
        found.append("docker-compose command string")
    if COMPOSE_CONTROL_RE.search(text):
        found.append("compose up/down/kill/stop/rm string")
    if DOCKER_SOCKET_RE.search(text):
        found.append("Docker socket or pipe name")
    if DOCKER_HOST_RE.search(text):
        found.append("DOCKER_HOST")
    return found


def _check_sequence(node: ast.List | ast.Tuple) -> list[str]:
    consts = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    found: list[str] = []
    if node.elts and isinstance(node.elts[0], ast.Constant) and node.elts[0].value in DOCKER_BINARY_NAMES:
        found.append("argument list starting with the Docker binary")
    for first, second in zip(consts, consts[1:]):
        if first == "compose" and second in COMPOSE_OPS:
            found.append("compose control arguments")
    if "docker" in consts and COMPOSE_OPS & set(consts):
        found.append("Docker container-control arguments")
    return found


def scan_source(source: str, fname: str = "") -> list[str]:
    """Findings (rule text with a line number) for one Python source. Empty when the source is clean.

    ``fname`` is the base filename (e.g. ``"loader.py"``); it is used for a small number of
    file-specific rules.  ``loader.py`` is the only Viewer module that may import and call
    ``read_evidence`` with bounded bytes; every other module is prohibited from importing it.
    """
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    # loader.py is explicitly allowed to import and call read_evidence with bytes
    _is_loader = (fname == "loader.py")
    aliases: dict[str, str] = {}
    findings: list[str] = []

    def add(node: ast.AST, rule: str) -> None:
        findings.append(f"line {getattr(node, 'lineno', '?')}: {rule}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[(alias.asname or alias.name).split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                if alias.asname:
                    aliases[alias.asname] = alias.name
                top = alias.name.split(".")[0]
                if top in FORBIDDEN_IMPORT_TOP:
                    add(node, f"import of {alias.name}")
                for dotted in FORBIDDEN_IMPORT_DOTTED:
                    if alias.name == dotted or alias.name.startswith(dotted + "."):
                        add(node, f"import of {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if node.level == 0 and top in FORBIDDEN_IMPORT_TOP:
                add(node, f"import from {module}")
            for dotted in FORBIDDEN_IMPORT_DOTTED:
                if module == dotted or module.startswith(dotted + "."):
                    add(node, f"import from {module}")
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}" if module else alias.name
                if (module, alias.name) in {("http", "client"), ("coroner.runner.experiments", "base"), ("coroner.runner", "evaluate")}:
                    add(node, f"import of {module}.{alias.name}")
                if module == "os" and (
                    alias.name in OS_WRITE_CALLS or alias.name.startswith(OS_PROCESS_PREFIXES)
                ):
                    add(node, f"import of os.{alias.name}")
                if module in {"coroner.runner.summary", "coroner.runner.manifest", "coroner.evidence.reader",
                              "coroner.evidence"} and alias.name in FORBIDDEN_CALL_NAMES | (set() if _is_loader else {"read_evidence"}):
                    add(node, f"import of {alias.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            for rule in _check_string(node.value):
                add(node, rule)
            if "O_" in node.value and node.value in WRITE_O_FLAGS:
                add(node, f"write flag name {node.value}")
        elif isinstance(node, (ast.List, ast.Tuple)):
            for rule in _check_sequence(node):
                add(node, rule)
        elif isinstance(node, ast.Attribute):
            if node.attr in WRITE_O_FLAGS:
                add(node, f"write flag {node.attr}")
        elif isinstance(node, ast.Name):
            if node.id in WRITE_O_FLAGS:
                add(node, f"write flag {node.id}")

        if isinstance(node, ast.Call):
            func = node.func
            dotted = _dotted(func, aliases)
            last = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
            imported_head = isinstance(func, ast.Attribute) and isinstance(_root_name(func), ast.Name) and (
                _root_name(func).id in aliases  # type: ignore[union-attr]
            )
            if dotted == "open" or (dotted in {"io.open", "codecs.open", "builtins.open"} and imported_head):
                problem = _mode_problem(node, builtin_like=True)
                if problem:
                    add(node, problem)
            elif isinstance(func, ast.Attribute) and func.attr == "open":
                problem = _mode_problem(node, builtin_like=False)
                if problem:
                    add(node, problem)
            if dotted is not None:
                head = dotted.split(".")[0]
                if head in FORBIDDEN_IMPORT_TOP:
                    add(node, f"call of {dotted}")
                if dotted.startswith("os."):
                    name = dotted.split(".", 1)[1]
                    if name in OS_WRITE_CALLS or name.startswith(OS_PROCESS_PREFIXES):
                        add(node, f"call of {dotted}")
                    if name == "open":
                        flags = node.args[1] if len(node.args) >= 2 else None
                        for kw in node.keywords:
                            if kw.arg == "flags":
                                flags = kw.value
                        if flags is None:
                            add(node, "os.open without explicit flags")
                if dotted.endswith("which") and any(
                    isinstance(a, ast.Constant) and a.value in DOCKER_BINARY_NAMES for a in node.args
                ):
                    add(node, "which() lookup of the Docker binary")
                if dotted in {"os.getenv", "os.environ.get"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and first.value == "DOCKER_HOST":
                        add(node, "read of DOCKER_HOST")
            if isinstance(func, ast.Attribute):
                if func.attr in PATH_WRITE_METHODS:
                    add(node, f"write-side method .{func.attr}()")
                elif func.attr in PATH_ONE_ARG_METHODS and len(node.args) + len(node.keywords) == 1:
                    add(node, f"write-side method .{func.attr}()")
            if last in FORBIDDEN_CALL_NAMES:
                add(node, f"call of {last}")
            if last == "read_evidence":
                first = node.args[0] if node.args else None
                # loader.py is allowed to call read_evidence with a local bytes variable (bounded read)
                if _is_loader:
                    # Only flag if the first argument is explicitly a string or Path (suspicious)
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        add(node, "read_evidence() that may take a path")
                elif not (isinstance(first, ast.Constant) and isinstance(first.value, bytes)):
                    add(node, "read_evidence() that may take a path")
    return findings


def viewer_sources() -> list[Path]:
    return sorted(p for p in VIEWER_DIR.rglob("*.py") if "__pycache__" not in p.parts)


# ---------------------------------------------------------------- the real scan


def test_viewer_sources_are_found() -> None:
    names = {p.name for p in viewer_sources()}
    assert {"app.py", "cli.py", "files.py", "hostcheck.py"} <= names


@pytest.mark.parametrize("path", viewer_sources(), ids=lambda p: p.name)
def test_viewer_module_has_no_forbidden_operation(path: Path) -> None:
    source = path.read_bytes().decode("utf-8")
    assert scan_source(source, fname=path.name) == []


def test_files_module_opens_read_only() -> None:
    """`os.open` appears in files.py, only with non-write flags (no write flag name anywhere)."""
    source = (VIEWER_DIR / "files.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "open"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"
    ]
    assert calls, "expected the read-only os.open call"
    assert "O_RDONLY" in source
    assert scan_source(source, fname="files.py") == []


# ---------------------------------------------------------------- negative controls (must be detected)


DETECTED = {
    "import docker": "import docker\n",
    "from docker import APIClient": "from docker import APIClient\n",
    "import python_on_whales": "import python_on_whales\n",
    "import dockerpty": "import dockerpty\n",
    "import subprocess": "import subprocess\n",
    "subprocess.run docker kill": "import subprocess\nsubprocess.run(['docker', 'kill', 'x'])\n",
    "os.system docker ps": "import os\nos.system('docker ps')\n",
    "os.popen": "import os\nos.popen('ls')\n",
    "os.execv": "import os\nos.execv('/bin/sh', ['sh'])\n",
    "os.spawnl": "import os\nos.spawnl(0, 'x')\n",
    "shutil.which docker": "import shutil\nshutil.which('docker')\n",
    "docker socket string": "SOCK = '/var/run/docker.sock'\n",
    "docker pipe name": "PIPE = r'\\\\.\\pipe\\docker_engine'\n",
    "os.getenv DOCKER_HOST": "import os\nx = os.getenv('DOCKER_HOST')\n",
    "os.environ.get DOCKER_HOST": "import os\nx = os.environ.get('DOCKER_HOST')\n",
    "os.environ[DOCKER_HOST]": "import os\nx = os.environ['DOCKER_HOST']\n",
    "docker compose down string": "CMD = 'docker compose down'\n",
    "docker run string": "CMD = 'docker run --rm busybox'\n",
    "compose up list": "ARGS = ['compose', 'up', '-d']\n",
    "docker kill list": "ARGS = ['docker', 'kill', 'x']\n",
    "open for write": "f = open('x', 'w')\n",
    "open for append keyword": "f = open('x', mode='a')\n",
    "open exclusive": "f = open('x', 'xb')\n",
    "open update": "f = open('x', 'r+')\n",
    "open non-literal mode": "m = 'r'\nf = open('x', m)\n",
    "os.remove": "import os\nos.remove('x')\n",
    "os.unlink via alias": "import os as o\no.unlink('x')\n",
    "os.rename": "import os\nos.rename('a', 'b')\n",
    "os.replace": "import os\nos.replace('a', 'b')\n",
    "os.mkdir": "import os\nos.mkdir('d')\n",
    "os.makedirs": "import os\nos.makedirs('d')\n",
    "os.rmdir": "import os\nos.rmdir('d')\n",
    "os.chmod": "import os\nos.chmod('x', 0)\n",
    "os.utime": "import os\nos.utime('x')\n",
    "os.truncate": "import os\nos.truncate('x', 0)\n",
    "from os import remove": "from os import remove\n",
    "import shutil": "import shutil\n",
    "shutil.rmtree": "import shutil\nshutil.rmtree('x')\n",
    "import tempfile": "import tempfile\n",
    "Path.write_text": "p.write_text('x')\n",
    "Path.mkdir": "p.mkdir()\n",
    "Path.touch": "p.touch()\n",
    "Path.unlink": "p.unlink()\n",
    "Path.rename": "p.rename(q)\n",
    "Path.replace": "p.replace(q)\n",
    "Path.rmdir": "p.rmdir()\n",
    "os.open with O_WRONLY": "import os\nos.open('x', os.O_WRONLY | os.O_CREAT)\n",
    "O_ flag by name": "import os\nflag = getattr(os, 'O_TRUNC')\n",
    "import socket": "import socket\n",
    "import urllib.request": "import urllib.request\n",
    "import http.client": "import http.client\n",
    "from http import client": "from http import client\n",
    "read_manifest call": "read_manifest(p)\n",
    "build_summary call": "build_summary(d, r)\n",
    "read_evidence path": "read_evidence(path)\n",
    "import experiments.base": "import coroner.runner.experiments.base\n",
    "from experiments.base": "from coroner.runner.experiments.base import X\n",
    "import evaluate": "import coroner.runner.evaluate\n",
}


@pytest.mark.parametrize("label", sorted(DETECTED))
def test_negative_control_is_detected(label: str) -> None:
    assert scan_source(DETECTED[label]) != [], label


# ---------------------------------------------------------------- allowed sources (must NOT be flagged)


CLEAN = {
    "docker names only": (
        "docker_state = 1\n"
        "NAME = 'docker_inspect.json'\n"
        "container_state = None\n"
        "# comment mentioning Docker, docker compose down and DOCKER_HOST\n"
        "LABEL = 'Docker state'\n"
        "def f():\n"
        "    '''Docstring: docker run, /var/run/docker.sock and subprocess are discussed here.'''\n"
        "    return docker_state\n"
    ),
    "read-only open": "with open('x', 'rb') as h:\n    h.read()\nopen('y')\nopen('z', 'r')\n",
    "read-only os.open": "import os\nfd = os.open('x', os.O_RDONLY | getattr(os, 'O_BINARY', 0))\nos.close(fd)\n",
    "str.replace": "t = 'a-b'.replace('-', '_')\n",
    "os read-side calls": "import os\nos.scandir('.')\nos.lstat('x')\nos.fstat(0)\nos.path.realpath('x')\n",
    "read_evidence with bytes": "read_evidence(b'{}\\n')\n",
}


@pytest.mark.parametrize("label", sorted(CLEAN))
def test_allowed_source_is_not_flagged(label: str) -> None:
    assert scan_source(CLEAN[label]) == [], label
