"""Guards that keep unit tests Docker-free and network-free (Requirement 13.8).

Pure helpers plus `install(monkeypatch)`, used by the autouse fixture in
tests/unit/conftest.py. Windows asyncio builds its self-pipe with socket.socketpair(),
which on Windows connects a loopback socket pair through socket.socket.connect, so
connects made inside socket.socketpair are allowed and nothing else is.
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import threading
from typing import Any

import pytest


class NetworkAccessBlocked(AssertionError):
    """A unit test tried to open a network connection or resolve a name."""


class DockerInvocationBlocked(AssertionError):
    """A unit test tried to run the Docker CLI."""


_DOCKER_NAMES = frozenset({"docker", "docker-compose"})
_EXEC_SUFFIXES = (".exe", ".cmd", ".bat", ".com")
_SHELL_DOCKER_RE = re.compile(
    r"""(^|[\s;&|()"'/\\])docker(-compose)?(\.exe|\.cmd|\.bat|\.com)?(?=$|[\s;&|()"'])""",
    re.IGNORECASE,
)
_state = threading.local()


def _program_name(token: Any) -> str:
    text = os.fsdecode(token).strip().strip("\"'")
    name = text.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in _EXEC_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def is_docker_command(args: Any, shell: bool = False, executable: Any = None) -> bool:
    """True when a Popen call would start the Docker CLI."""
    if executable is not None and _program_name(executable) in _DOCKER_NAMES:
        return True
    if isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args)
        if shell:
            return _SHELL_DOCKER_RE.search(text) is not None
        first = text.strip().split(None, 1)[0] if text.strip() else ""
        return _program_name(first) in _DOCKER_NAMES if first else False
    try:
        first_arg = next(iter(args))
    except (TypeError, StopIteration):
        return False
    return _program_name(first_arg) in _DOCKER_NAMES


def _inside_socketpair() -> bool:
    return getattr(_state, "depth", 0) > 0


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install all guards. monkeypatch undoes them at the end of the test."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_socketpair = socket.socketpair

    def blocked_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        raise NetworkAccessBlocked(f"unit tests must not resolve names: {host!r}")

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if not _inside_socketpair():
            raise NetworkAccessBlocked(f"unit tests must not connect: {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if not _inside_socketpair():
            raise NetworkAccessBlocked(f"unit tests must not connect: {address!r}")
        return real_connect_ex(self, address)

    def guarded_socketpair(*args: Any, **kwargs: Any) -> Any:
        _state.depth = getattr(_state, "depth", 0) + 1
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            _state.depth -= 1

    async def blocked_create_connection(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise NetworkAccessBlocked("unit tests must not call loop.create_connection")

    async def blocked_sock_connect(self: Any, sock: Any, address: Any) -> Any:
        raise NetworkAccessBlocked(f"unit tests must not call loop.sock_connect: {address!r}")

    monkeypatch.setattr(socket, "getaddrinfo", blocked_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "socketpair", guarded_socketpair)
    # asyncio connects through the event loop, which can skip socket.connect for numeric IPs.
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_connection", blocked_create_connection)
    monkeypatch.setattr(asyncio.selector_events.BaseSelectorEventLoop, "sock_connect", blocked_sock_connect)
    proactor = getattr(asyncio.proactor_events, "BaseProactorEventLoop", None)
    if proactor is not None:
        monkeypatch.setattr(proactor, "sock_connect", blocked_sock_connect)

    real_popen_init = subprocess.Popen.__init__

    def guarded_popen_init(self: subprocess.Popen, *args: Any, **kwargs: Any) -> None:
        command = args[0] if args else kwargs.get("args")
        shell = kwargs.get("shell", args[8] if len(args) > 8 else False)
        executable = kwargs.get("executable", args[2] if len(args) > 2 else None)
        if is_docker_command(command, bool(shell), executable):
            raise DockerInvocationBlocked(f"unit tests must not run the Docker CLI: {command!r}")
        real_popen_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init)