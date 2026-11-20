"""The Experiment_Runner command line (task 6.18).

    python -m coroner.runner run {a|b|c} [--runs-dir PATH] [--mem-limit SIZE] [--leak-chunk-bytes N]
    python -m coroner.runner summary RUN_ID [--runs-dir PATH]
    python -m coroner.runner clock-check [--count N] [--collector-url URL]

Contract: docs/m0-results/m0.6-runner-cli.md. `main` returns 0, 1 or 2 and never calls `sys.exit`. All output goes to the
injected `out` and `err` streams. Importing this module touches no network, Docker or filesystem.
"""
from __future__ import annotations

import argparse
import contextlib
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, TextIO

from coroner.common.envparse import ConfigError, parse_mem_size
from coroner.common.timeutil import format_utc_ms
from coroner.runner import summary
from coroner.runner.collector_client import CollectorClient, Transport
from coroner.runner.docker_port import DockerCli, DockerPort
from coroner.runner.evaluate import CLOCK_TOLERANCE_MS
from coroner.runner.experiments import exp_a, exp_b, exp_c
from coroner.runner.experiments.base import (
    DEFAULT_LEAK_CHUNK_BYTES,
    DEFAULT_MEM_LIMIT,
    EXIT_FAILED,
    EXIT_PASSED,
    ExperimentContext,
    ExperimentResult,
)
from coroner.runner.manifest import posix_path

__all__ = ["main", "run_clock_check"]

PROG = "python -m coroner.runner"
DEFAULT_RUNS_DIR = "runs"
DEFAULT_COLLECTOR_PORT = "8081"
DEFAULT_DEMO_PORT = "8080"
COLLECTOR_PORT_VAR = "CORONER_COLLECTOR_PORT"
DEMO_PORT_VAR = "CORONER_DEMO_PORT"
MIN_LEAK_CHUNK_BYTES = 1
MAX_LEAK_CHUNK_BYTES = 104_857_600
DEFAULT_CLOCK_COUNT = 20
MAX_CLOCK_COUNT = 1000
CLOCK_PAUSE_S = 0.1
USAGE_ERROR = 2


class _UsageError(Exception):
    """A command-line or environment value that is not acceptable (exit 2, message printed after `error: `)."""


class _Parser(argparse.ArgumentParser):
    """argparse that reports errors as `error: ...` plus usage on stderr and raises SystemExit(2)."""

    def error(self, message: str) -> Any:
        sys.stderr.write(f"error: {message}\n")
        sys.stderr.write(self.format_usage())
        raise SystemExit(USAGE_ERROR)


def _clock_count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if not 1 <= value <= MAX_CLOCK_COUNT:
        raise argparse.ArgumentTypeError(f"--count must be 1 to {MAX_CLOCK_COUNT}, got {value}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description="Run Coroner experiments, summarize stored runs, check clocks.")
    sub = parser.add_subparsers(dest="command", metavar="{run,summary,clock-check}", required=True)

    run = sub.add_parser("run", help="run experiment a, b or c")
    run.add_argument("experiment", choices=["a", "b", "c"])
    run.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, metavar="PATH")
    run.add_argument("--mem-limit", default=DEFAULT_MEM_LIMIT, metavar="SIZE")
    run.add_argument("--leak-chunk-bytes", type=int, default=DEFAULT_LEAK_CHUNK_BYTES, metavar="N")

    show = sub.add_parser("summary", help="summarize a stored run (read-only)")
    show.add_argument("run_id", metavar="RUN_ID")
    show.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, metavar="PATH")

    check = sub.add_parser("clock-check", help="measure the Collector clock offset")
    check.add_argument("--count", type=_clock_count, default=DEFAULT_CLOCK_COUNT, metavar="N")
    check.add_argument("--collector-url", default=None, metavar="URL")
    return parser


def _port(env: Mapping[str, str], name: str, default: str) -> str:
    if name not in env:
        return default
    value = env[name]
    if not (value.isascii() and value.isdigit() and 1 <= int(value) <= 65535):
        raise _UsageError(f"invalid {name} {value!r} (expected a port number 1 to 65535)")
    return value


def _validate_run_args(args: argparse.Namespace) -> None:
    try:
        size = parse_mem_size(args.mem_limit)
    except ConfigError as exc:
        raise _UsageError(f"invalid --mem-limit: {exc}") from exc
    if size <= 0:
        raise _UsageError(f"invalid --mem-limit {args.mem_limit!r} (must be greater than zero)")
    if not MIN_LEAK_CHUNK_BYTES <= args.leak_chunk_bytes <= MAX_LEAK_CHUNK_BYTES:
        raise _UsageError(
            f"invalid --leak-chunk-bytes {args.leak_chunk_bytes} "
            f"(expected {MIN_LEAK_CHUNK_BYTES} to {MAX_LEAK_CHUNK_BYTES})"
        )


def _run_command(
    args: argparse.Namespace,
    *,
    docker: DockerPort | None,
    collector: CollectorClient | None,
    http: Transport | None,
    wall_clock: Callable[[], datetime] | None,
    monotonic: Callable[[], float] | None,
    sleep: Callable[[float], None] | None,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
) -> int:
    _validate_run_args(args)
    collector_port = _port(env, COLLECTOR_PORT_VAR, DEFAULT_COLLECTOR_PORT)
    demo_port = _port(env, DEMO_PORT_VAR, DEFAULT_DEMO_PORT)
    injected = dict(http=http, wall_clock=wall_clock, monotonic=monotonic, sleep=sleep)
    kwargs = {k: v for k, v in injected.items() if v is not None}
    docker_port = docker if docker is not None else DockerCli()
    collector_client = (
        collector
        if collector is not None
        else CollectorClient(f"http://127.0.0.1:{collector_port}", transport=http, wall_clock=wall_clock)
    )
    ctx = ExperimentContext(
        experiment=args.experiment,
        docker=docker_port,
        collector=collector_client,
        runs_dir=args.runs_dir,
        mem_limit=args.mem_limit,
        leak_chunk_bytes=args.leak_chunk_bytes,
        demo_url=f"http://127.0.0.1:{demo_port}",
        **kwargs,
    )
    module = {"a": exp_a, "b": exp_b, "c": exp_c}[args.experiment]
    try:
        result: ExperimentResult = module.run(ctx)
    except KeyboardInterrupt:
        err.write("Interrupted. The run manifest was marked aborted.\n")
        return EXIT_FAILED
    if result.outcome == "not_started":
        err.write(result.line + "\n")
        return result.exit_code
    out.write(result.line + "\n")
    if result.manifest_path is not None:
        out.write("Manifest: " + posix_path(result.manifest_path) + "\n")
    if result.failure is not None:
        out.write(f"Failure: {result.failure.code}: {result.failure.message}\n")
    return result.exit_code


def _summary_command(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        built = summary.build_summary(args.runs_dir, args.run_id)
    except summary.SummaryError as exc:
        err.write("error: " + " ".join(str(exc).split()) + "\n")
        return USAGE_ERROR
    out.write(summary.render_summary(built))
    return summary.summary_exit_code(built)


def _signed(value: int) -> str:
    return f"{value:+d}"


def run_clock_check(
    collector: CollectorClient,
    *,
    count: int,
    wall_clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    out: TextIO,
) -> int:
    """Take `count` sequential health measurements and print the offset report (contract section 5)."""
    url = getattr(collector, "base_url", "unknown")
    measured_at = format_utc_ms(wall_clock())
    usable = []
    failures = 0
    last_error = "no error text"
    for index in range(count):
        if index > 0:
            sleep(CLOCK_PAUSE_S)
        health = collector.health()
        if health.clock_offset is not None:
            usable.append(health.clock_offset)
            continue
        failures += 1
        last_error = health.clock_offset_error or health.error or (
            f"HTTP status {health.http_status}" if health.http_status is not None else "no response"
        )
    if not usable:
        out.write(f"error: no usable clock measurement from {url} ({count} attempts): {last_error}\n")
        return USAGE_ERROR

    best = usable[0]
    for item in usable[1:]:
        if item.round_trip_ms < best.round_trip_ms:
            best = item
    offsets = [item.offset_ms for item in usable]
    trips = [item.round_trip_ms for item in usable]
    lines = [
        f"{'clock-check:':<16}collector={url}",
        f"{'Measured at:':<16}{measured_at}",
        f"{'Measurements:':<16}{len(usable)} of {count} usable",
    ]
    lines += [
        f"{'Offset:':<16}{_signed(best.offset_ms)} ms (positive = Collector clock ahead of host; "
        "host time = collector time - offset)",
        f"{'Uncertainty:':<16}{best.uncertainty_ms:.1f} ms (half the round trip of the best measurement)",
        f"{'Offset median:':<16}{_signed(statistics.median_low(offsets))} ms",
        f"{'Offset range:':<16}{_signed(min(offsets))} ms to {_signed(max(offsets))} ms "
        f"(spread {max(offsets) - min(offsets)} ms)",
        f"{'Round trip:':<16}min {min(trips):.1f} ms, median {statistics.median(trips):.1f} ms, "
        f"max {max(trips):.1f} ms",
        f"{'Tolerance:':<16}{CLOCK_TOLERANCE_MS} ms (documented Experiment B clock tolerance; never changed "
        "automatically)",
    ]
    if failures:
        lines.append(f"{'Failures:':<16}{failures} (last: {last_error})")
    if abs(best.offset_ms) + best.uncertainty_ms > CLOCK_TOLERANCE_MS:
        lines.append(
            f"{'Warning:':<16}offset plus uncertainty exceeds the {CLOCK_TOLERANCE_MS} ms tolerance; "
            "ask before changing the tolerance"
        )
    out.write("\n".join(lines) + "\n")
    return EXIT_PASSED


def _clock_command(
    args: argparse.Namespace,
    *,
    collector: CollectorClient | None,
    http: Transport | None,
    wall_clock: Callable[[], datetime] | None,
    sleep: Callable[[float], None] | None,
    env: Mapping[str, str],
    out: TextIO,
) -> int:
    clock = wall_clock if wall_clock is not None else (lambda: datetime.now(timezone.utc))
    client = collector
    if client is None:
        url = args.collector_url
        if url is None:
            url = f"http://127.0.0.1:{_port(env, COLLECTOR_PORT_VAR, DEFAULT_COLLECTOR_PORT)}"
        client = CollectorClient(url, transport=http, wall_clock=wall_clock)
    return run_clock_check(
        client,
        count=args.count,
        wall_clock=clock,
        sleep=sleep if sleep is not None else time.sleep,
        out=out,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    docker: DockerPort | None = None,
    collector: CollectorClient | None = None,
    http: Transport | None = None,
    wall_clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run one command and return its exit code (0, 1 or 2)."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if env is None:
        import os

        env = os.environ
    parser = _build_parser()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else USAGE_ERROR
    try:
        if args.command == "run":
            return _run_command(
                args, docker=docker, collector=collector, http=http, wall_clock=wall_clock,
                monotonic=monotonic, sleep=sleep, env=env, out=out, err=err,
            )
        if args.command == "summary":
            return _summary_command(args, out, err)
        return _clock_command(
            args, collector=collector, http=http, wall_clock=wall_clock, sleep=sleep, env=env, out=out
        )
    except _UsageError as exc:
        err.write(f"error: {exc}\n")
        return USAGE_ERROR
    except KeyboardInterrupt:
        err.write("Interrupted.\n")
        return EXIT_FAILED
    except Exception as exc:
        if args.command == "run":
            raise
        err.write(f"error: {type(exc).__name__}: {exc}\n")
        return USAGE_ERROR
