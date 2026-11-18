"""Probe: configuration, real readers, absolute-deadline sampler, one-slot sender (Requirements 2, 3, 7).

Standard library only. No reader has a synthetic fallback, and none returns a previous
value: a reader that cannot read raises (RSS) or reports "none" (container id).
"""
from __future__ import annotations

import asyncio
import http.client
import json
import math
import mmap
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

MAX_DROPPED = 4294967295
DEFAULT_COLLECTOR_URL = "http://collector:8081"
DEFAULT_INTERVAL_MS = 500
MIN_INTERVAL_MS = 100
MAX_INTERVAL_MS = 60000

# Identical to coroner.evidence.schema.RUN_ID_RE (a unit test asserts this). The Probe may
# not import coroner.evidence, so the pattern is copied. Always use fullmatch.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIGITS_RE = re.compile(r"[0-9]+")
_MOUNTINFO_RE = re.compile(r"/containers/([0-9a-f]{64})/")
_CGROUP_RE = re.compile(r"(?:/docker/|/docker-)([0-9a-f]{64})(?:\.scope)?(?:/|$)")
_SHORT_ID_RE = re.compile(r"[0-9a-f]{12}")

NO_CONTAINER_ID_REASON = "no container id found in mountinfo, cgroup, or HOSTNAME"


@dataclass(frozen=True)
class ProbeConfig:
    run_id: str
    collector_url: str
    interval_ms: int
    send_timeout_s: float = 0.200
    start_deadline_s: float = 0.100


def load_config(env: Mapping[str, str], warn: Callable[[str], None]) -> ProbeConfig:
    """Build a ProbeConfig from `env` only (never os.environ; `env` is not mutated).

    An empty or missing CORONER_RUN_ID generates `manual-<8 hex>`. A supplied run id that
    is not a valid Run_Identifier raises ValueError naming it. The collector URL is passed
    through unvalidated. A missing interval is 500 silently. A supplied interval that is
    not 100..60000 as plain ASCII digits (so "", " 100", "+100", "1.5" are all invalid)
    is 500 with exactly one warn() call.
    """
    run_id = env.get("CORONER_RUN_ID", "")
    if run_id == "":
        run_id = "manual-" + secrets.token_hex(4)
    elif not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"invalid CORONER_RUN_ID={run_id!r} (expected {_RUN_ID_RE.pattern})")
    url = env.get("CORONER_COLLECTOR_URL", DEFAULT_COLLECTOR_URL)
    interval = DEFAULT_INTERVAL_MS
    raw = env.get("CORONER_SAMPLE_INTERVAL_MS")
    if raw is not None:
        value = int(raw) if raw.isascii() and _DIGITS_RE.fullmatch(raw) and len(raw) <= 9 else None
        if value is not None and MIN_INTERVAL_MS <= value <= MAX_INTERVAL_MS:
            interval = value
        else:
            warn(
                f"invalid CORONER_SAMPLE_INTERVAL_MS={raw!r} (expected an integer from "
                f"{MIN_INTERVAL_MS} to {MAX_INTERVAL_MS}); using {DEFAULT_INTERVAL_MS}"
            )
    return ProbeConfig(run_id=run_id, collector_url=url, interval_ms=interval)


def _page_size() -> int:
    sysconf = getattr(os, "sysconf", None)
    return int(sysconf("SC_PAGE_SIZE")) if sysconf is not None else mmap.PAGESIZE


def read_rss_bytes(
    path: str | os.PathLike[str] = "/proc/self/statm",
    page_size: Callable[[], int] = _page_size,
) -> int:
    """Current resident set size: resident pages (statm field 2) times the page size.

    Raises OSError if the file cannot be read and ValueError if it is malformed.
    """
    text = Path(path).read_text(encoding="ascii")
    fields = text.split()
    if len(fields) < 2 or not (fields[1].isascii() and _DIGITS_RE.fullmatch(fields[1])):
        raise ValueError(f"malformed statm content in {os.fspath(path)}: {text[:80]!r}")
    return int(fields[1]) * page_size()


def _read_lines(path: str | os.PathLike[str]) -> list[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def read_container_id(
    mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
    cgroup_path: str | os.PathLike[str] = "/proc/self/cgroup",
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, str]:
    """Return (container_id, container_id_source).

    Order: mountinfo (64 hex in `/containers/<id>/`), cgroup (`/docker/<id>` or
    `docker-<id>.scope`), HOSTNAME (exactly 12 lowercase hex, source "hostname"), then
    (None, "none"). Unreadable files fall through. Nothing is padded or invented. When
    the source is "none" the Sample's null reason is NO_CONTAINER_ID_REASON.
    """
    for line in _read_lines(mountinfo_path):
        match = _MOUNTINFO_RE.search(line)
        if match:
            return match.group(1), "mountinfo"
    for line in _read_lines(cgroup_path):
        match = _CGROUP_RE.search(line.split(":", 2)[-1])
        if match:
            return match.group(1), "cgroup"
    hostname = (os.environ if env is None else env).get("HOSTNAME", "")
    if _SHORT_ID_RE.fullmatch(hostname):
        return hostname, "hostname"
    return None, "none"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"expected a non-negative integer, got {value!r}")
    return value


class _HttpTransport:
    """POST `{collector_url}/v1/samples` (http only, no redirects); True only for HTTP 200.

    Each phase gets a socket timeout of the remaining budget. Limit: DNS (getaddrinfo) ignores socket
    timeouts, so a stalled resolver can overrun; the sender then refuses to acknowledge a late result.
    """

    def __init__(self, url: str) -> None:
        self._url = url

    def send(self, body: bytes, timeout_s: float) -> bool:
        end, conn = time.monotonic() + timeout_s, None

        def left() -> float:
            if (rest := end - time.monotonic()) <= 0:
                raise TimeoutError("send budget exhausted")
            return rest

        try:
            url = urlsplit(self._url)
            if url.scheme != "http" or not url.hostname:
                return False
            conn = http.client.HTTPConnection(url.hostname, url.port, timeout=left())
            conn.connect()
            conn.sock.settimeout(left())
            headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
            conn.request("POST", url.path.rstrip("/") + "/v1/samples", body, headers)
            conn.sock.settimeout(left())
            status = conn.getresponse().status
            left()
            return status == 200
        except Exception:
            return False
        finally:
            if conn is not None:
                conn.close()


class Probe:
    """Sampler plus one daemon sender thread (Requirements 2.x, 3.1 to 3.7, 7.2, 7.6).

    An unbuildable Sample (failing pid, naive clock) is skipped without advancing `seq`. `offer` serialises
    once, calls `on_sample` (a raise drops the Sample unoffered), then hands off to the one-slot sender; a
    busy sender drops the new Sample. Each post-`seq` failure adds exactly one to `dropped`; only HTTP 200
    within the deadline subtracts the carried value. No queue, retry, atexit or signal hook.
    """

    def __init__(
        self,
        config: ProbeConfig,
        *,
        transport: object | None = None,
        rss_reader: Callable[[], int] = read_rss_bytes,
        pid_reader: Callable[[], int] = os.getpid,
        container_id_reader: Callable[[], tuple[str | None, str]] = read_container_id,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
        on_sample: Callable[[dict], None] | None = None,
    ) -> None:
        self._config, self._on_sample = config, on_sample
        self._transport = transport if transport is not None else _HttpTransport(config.collector_url)
        self._slot: tuple[bytes, int, float] | None = None  # (body, carried dropped, handoff time)
        self._thread: threading.Thread | None = None
        self._wake, self._exit = threading.Event(), threading.Event()
        self._rss, self._pid, self._container = rss_reader, pid_reader, container_id_reader
        self._monotonic, self._wall_clock = monotonic, wall_clock
        self._lock = threading.Lock()
        self._seq = self._requests = self._dropped = 0
        self._task: asyncio.Task | None = None

    def count_request(self) -> None:
        with self._lock:
            self._requests += 1

    @property
    def state(self) -> dict:
        with self._lock:
            return {"next_seq": self._seq, "dropped": self._dropped}

    def build_sample(self, loop_delay_ms: float | None, loop_delay_reason: str | None = None) -> dict:
        """One Sample. Raises ValueError/OSError for an unusable required value; `seq` then stays put."""
        reasons: dict[str, str] = {}
        if loop_delay_ms is None:
            if not isinstance(loop_delay_reason, str) or not 1 <= len(loop_delay_reason) <= 200:
                raise ValueError("loop_delay_ms is None without a reason of 1 to 200 characters")
            reasons["loop_delay_ms"] = loop_delay_reason
        elif isinstance(loop_delay_ms, bool) or not isinstance(loop_delay_ms, (int, float)) or not (
            math.isfinite(loop_delay_ms) and loop_delay_ms >= 0
        ):
            raise ValueError(f"loop_delay_ms must be a finite number >= 0, got {loop_delay_ms!r}")
        pid = _count(self._pid())
        ts = self._wall_clock()
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("wall clock returned a timezone-naive datetime")
        ts = ts.astimezone(timezone.utc)
        ts_text = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"
        try:
            rss: int | None = _count(self._rss())
        except Exception as exc:
            rss, reasons["rss_bytes"] = None, f"rss unavailable: {type(exc).__name__}"[:200]
        try:
            container_id, source = self._container()
            if container_id is None or source == "none":
                container_id, source = None, "none"
                reasons["container_id"] = NO_CONTAINER_ID_REASON
        except Exception as exc:
            container_id, source = None, "none"
            reasons["container_id"] = f"container id unavailable: {type(exc).__name__}"[:200]
        with self._lock:
            sample = {
                "run_id": self._config.run_id, "seq": self._seq, "ts": ts_text, "rss_bytes": rss,
                "request_count": self._requests, "loop_delay_ms": loop_delay_ms, "pid": pid,
                "container_id": container_id, "container_id_source": source,
                "interval_ms": self._config.interval_ms, "dropped": self._dropped,
            }
            if reasons:
                sample["null_reasons"] = reasons
            self._seq += 1
        return sample

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if self._task is None or self._task.done():
            self._task = (loop or asyncio.get_running_loop()).create_task(self._run())
        self._start_sender()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        thread = self._thread
        self._exit.set()
        self._wake.set()
        if thread is not None:
            await asyncio.to_thread(thread.join, 0.5)  # bounded; nothing is flushed

    def _start_sender(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._exit.is_set():
                return
            self._wake, self._exit = threading.Event(), threading.Event()
            self._thread = threading.Thread(
                target=self._send_loop, args=(self._wake, self._exit), name="coroner-probe-sender", daemon=True)
            self._thread.start()

    def offer(self, sample: dict) -> None:
        """Hand one built Sample to the sender without blocking; any failure or a busy sender counts one drop."""
        try:
            body = json.dumps(sample, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if self._on_sample is not None:
                self._on_sample(sample)
        except Exception:
            body = None
        with self._lock:
            if body is None or self._slot is not None:
                self._dropped = min(MAX_DROPPED, self._dropped + 1)
            else:
                self._slot = (body, sample["dropped"], self._monotonic())
                self._wake.set()

    def _send_loop(self, wake: threading.Event, exit_: threading.Event) -> None:
        while not exit_.is_set():
            wake.wait()
            wake.clear()
            with self._lock:
                slot = self._slot  # stays occupied while in flight, so a new Sample is refused
            if slot is None:
                continue
            body, carried, handed_off = slot
            deadline = handed_off + self._config.send_timeout_s
            try:
                remaining = deadline - self._monotonic()
                acked = remaining > 0 and self._transport.send(body, remaining) is True and self._monotonic() <= deadline
            except Exception:
                acked = False
            with self._lock:
                self._slot = None
                self._dropped = max(0, self._dropped - carried) if acked else min(MAX_DROPPED, self._dropped + 1)

    async def _run(self) -> None:
        interval, now = self._config.interval_ms / 1000, self._monotonic
        t0 = now()
        k = 1
        while True:
            deadline = t0 + k * interval
            while (current := now()) < deadline:
                await asyncio.sleep(deadline - current)
            try:
                sample = self.build_sample(max(0.0, (current - deadline) * 1000))
            except Exception:
                sample = None  # no Sample for this tick; seq was not advanced; carry on at the next deadline
            if sample is not None:
                self.offer(sample)
            k = int((current - t0) / interval) + 1
            while k > 1 and t0 + (k - 1) * interval > current:
                k -= 1
            while t0 + k * interval <= current:
                k += 1
