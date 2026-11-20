"""Tests for the unit-test guards (Requirement 13.8): no network, no Docker CLI."""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys

import pytest

from tests.unit.guards import DockerInvocationBlocked, NetworkAccessBlocked, is_docker_command


# --- network guards -------------------------------------------------------------


def test_socket_connect_is_blocked() -> None:
    with socket.socket() as sock:
        with pytest.raises(NetworkAccessBlocked):
            sock.connect(("127.0.0.1", 9))


def test_socket_connect_ex_is_blocked() -> None:
    with socket.socket() as sock:
        with pytest.raises(NetworkAccessBlocked):
            sock.connect_ex(("127.0.0.1", 9))


def test_name_resolution_is_blocked() -> None:
    with pytest.raises(NetworkAccessBlocked):
        socket.getaddrinfo("example.com", 80)


def test_create_connection_is_blocked() -> None:
    with pytest.raises(NetworkAccessBlocked):
        socket.create_connection(("example.com", 80), timeout=1)


def test_asyncio_loop_create_connection_is_blocked_even_for_numeric_ip() -> None:
    async def main() -> None:
        await asyncio.get_running_loop().create_connection(asyncio.Protocol, "127.0.0.1", 9)

    with pytest.raises(NetworkAccessBlocked):
        asyncio.run(main())


def test_asyncio_loop_sock_connect_is_blocked() -> None:
    async def main() -> None:
        with socket.socket() as sock:
            sock.setblocking(False)
            await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", 9))

    with pytest.raises(NetworkAccessBlocked):
        asyncio.run(main())


def test_socketpair_still_works() -> None:
    """Windows asyncio builds its self-pipe from a socket pair, so it must stay usable."""
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"x")
        assert right.recv(1) == b"x"


def test_asyncio_event_loop_still_runs_under_the_guards() -> None:
    async def main() -> int:
        await asyncio.sleep(0)
        return 42

    assert asyncio.run(main()) == 42


def test_connect_is_blocked_again_after_a_socketpair() -> None:
    left, right = socket.socketpair()
    left.close()
    right.close()
    with socket.socket() as sock:
        with pytest.raises(NetworkAccessBlocked):
            sock.connect(("127.0.0.1", 9))


# --- Docker CLI guards ----------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ["docker", "info"],
        ["docker.exe", "version"],
        [r"C:\Program Files\Docker\Docker\resources\bin\docker.exe", "ps"],
        ["/usr/bin/docker", "ps"],
        ["docker-compose", "up"],
        ["DOCKER", "info"],
    ],
)
def test_popen_of_docker_is_blocked(command: list[str]) -> None:
    with pytest.raises(DockerInvocationBlocked):
        subprocess.Popen(command)


def test_subprocess_run_of_docker_is_blocked() -> None:
    with pytest.raises(DockerInvocationBlocked):
        subprocess.run(["docker", "version"], capture_output=True)


@pytest.mark.parametrize("command", ["docker compose ps", "echo hi && docker ps", 'cd x; "docker.exe" info'])
def test_shell_command_strings_that_run_docker_are_blocked(command: str) -> None:
    with pytest.raises(DockerInvocationBlocked):
        subprocess.Popen(command, shell=True)


def test_docker_given_as_executable_argument_is_blocked() -> None:
    with pytest.raises(DockerInvocationBlocked):
        subprocess.Popen(["anything"], executable="docker")


def test_non_docker_subprocess_is_not_blocked() -> None:
    proc = subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "1"


@pytest.mark.parametrize(
    "command, expected",
    [
        (["docker", "info"], True),
        (["docker-compose", "up"], True),
        (["mydocker", "info"], False),
        (["dockerfile-lint"], False),
        ([sys.executable, "-c", "pass"], False),
        ([], False),
        ("docker info", True),
        ("python -c pass", False),
    ],
)
def test_is_docker_command(command: object, expected: bool) -> None:
    assert is_docker_command(command) is expected


def test_is_docker_command_shell_search_ignores_docker_in_other_words() -> None:
    assert is_docker_command("echo dockerfile", shell=True) is False
    assert is_docker_command("echo a && docker ps", shell=True) is True