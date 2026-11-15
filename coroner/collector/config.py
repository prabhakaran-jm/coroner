"""Collector configuration (Requirements 4.10, 4.11, 4.12; design "Durability levels").

`load_config` is pure: it reads only the mapping it is given, never `os.environ`, and does
not touch the filesystem. The evidence directory is created and checked at startup by the
run registry (Task 3.2), not here. Invalid settings raise `ConfigError`; the entry point
uses `load_config_or_exit` to turn that into a non-zero exit with a message.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from coroner.common.envparse import ConfigError, parse_int_env
from coroner.evidence.writer import Durability, parse_durability

DEFAULT_EVIDENCE_DIR = Path("/data/evidence")
DEFAULT_MAX_BODY_BYTES = 65536
DEFAULT_WATCHDOG_TICK_MS = 50
MAX_WATCHDOG_TICK_MS = 99  # design: the tick is always less than the 100 ms minimum Sample_Interval
CONFIG_ERROR_EXIT_CODE = 2

ENV_DURABILITY = "CORONER_DURABILITY"
ENV_MAX_BODY_BYTES = "CORONER_MAX_BODY_BYTES"
ENV_WATCHDOG_TICK_MS = "CORONER_WATCHDOG_TICK_MS"


@dataclass(frozen=True)
class CollectorConfig:
    durability: Durability
    max_body_bytes: int
    watchdog_tick_ms: int
    evidence_dir: Path


def load_config(env: Mapping[str, str], *, evidence_dir: Path | str = DEFAULT_EVIDENCE_DIR) -> CollectorConfig:
    """Build a CollectorConfig from `env`.

    Unset or empty `CORONER_DURABILITY` is `crash`; unset, empty, or all-whitespace numeric
    settings use their defaults. Any other invalid value raises ConfigError naming it.
    `evidence_dir` is a parameter, not an environment variable, so tests can point at a
    temporary directory.
    """
    return CollectorConfig(
        durability=parse_durability(env.get(ENV_DURABILITY)),
        max_body_bytes=parse_int_env(
            ENV_MAX_BODY_BYTES, env.get(ENV_MAX_BODY_BYTES), default=DEFAULT_MAX_BODY_BYTES, minimum=1
        ),
        watchdog_tick_ms=parse_int_env(
            ENV_WATCHDOG_TICK_MS, env.get(ENV_WATCHDOG_TICK_MS), default=DEFAULT_WATCHDOG_TICK_MS,
            minimum=1, maximum=MAX_WATCHDOG_TICK_MS,
        ),
        evidence_dir=Path(evidence_dir),
    )


def load_config_or_exit(
    env: Mapping[str, str],
    *,
    evidence_dir: Path | str = DEFAULT_EVIDENCE_DIR,
    stderr: TextIO | None = None,
) -> CollectorConfig:
    """Like `load_config`, but a ConfigError prints `coroner-collector: <message>` and exits with code 2."""
    try:
        return load_config(env, evidence_dir=evidence_dir)
    except ConfigError as exc:
        print(f"coroner-collector: {exc}", file=stderr if stderr is not None else sys.stderr)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from exc
