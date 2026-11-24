"""Artifact bytes → RunRecord / ListFacts, run listing, cache and semaphore.

This module converts the per-run artifact bytes returned by ``files.py``
into the parsed :class:`RunRecord` and :class:`ListFacts` structures that
the serialisers in ``model.py`` consume.

Cache limitation (R-F):
    The listing path keeps a stat-signature cache keyed by
    ``(size, mtime_ns, dev, ino)``.  A rewrite that keeps the same size,
    modification time, and file identity (deliberate metadata forgery) will
    be served from the stale cache entry without re-parsing.  The cache is
    discarded when the Viewer process restarts.  The Viewer never claims the
    cache prevents all stale reads; it documents this limitation explicitly.

Import constraints: stdlib only, plus coroner.runner.manifest, coroner.evidence.reader,
coroner.common.timeutil (via the other viewer modules), and coroner.viewer modules.
Never imports coroner.runner.experiments.base, coroner.runner.evaluate, or any other
heavy M0 module.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Any, Callable

from coroner.evidence.reader import UNOBSERVED_NOTE, ReadResult, RunSummary, read_evidence, summarize
from coroner.runner.manifest import (
    InspectError,
    ManifestError,
    RunManifest,
    deserialize_manifest,
    extract_inspect_excerpt,
    validate_run_id_for_path,
)

from coroner.viewer.classify import (
    experiment_type as classify_experiment_type,
    resolve_docker_state_with_fault,
    verdict_of,
)
from coroner.viewer.clocks import (
    DerivedBlock,
    OffsetResolution,
    axis_mode,
    derived_intervals,
    resolve_offset,
    tolerance_status,
)
from coroner.viewer.files import (
    ArtifactFault,
    ArtifactIO,
    ArtifactName,
    ArtifactRead,
    ArtifactStatus,
    RunEntry,
    RunsRoot,
    STATUS_OF,
    artifact_signature,
    find_run,
    list_run_entries,
    read_artifact,
)
from coroner.viewer.model import ArtifactState, ListFacts, RunRecord
from coroner.viewer.samples import (
    extract_samples,
    last_persisted,
    latest_capture,
    rss_stats,
)
from coroner.viewer.timefmt import epoch_ms_docker, epoch_ms_utc
from coroner.viewer.timeline import CHART_CAP, GapIndex, build_chart, gap_index

def _raw_finished_at(inspect_bytes: bytes | None) -> str | None:
    """Raw ``State.FinishedAt`` string from the bounded inspect bytes, or None.

    Accepts the one-element list form and the object form. The zero date and any
    unparseable value (per the local ``parse_docker_time``) count as not recorded.
    """
    if inspect_bytes is None:
        return None
    try:
        doc = json.loads(inspect_bytes)
    except (ValueError, TypeError):
        return None
    if isinstance(doc, list):
        doc = doc[0] if len(doc) == 1 else None
    if not isinstance(doc, dict):
        return None
    state = doc.get("State")
    if not isinstance(state, dict):
        return None
    value = state.get("FinishedAt")
    if epoch_ms_docker(value) is None:
        return None
    return value


__all__ = [
    "MAX_LISTED_RUNS",
    "RunCache",
    "load_detail",
    "load_listing",
]

_LOG = logging.getLogger("coroner.viewer.loader")

MAX_LISTED_RUNS = 500
_RACILY_CLEAN_NS = 2_000_000_000  # 2 seconds in nanoseconds

# Semaphore limits concurrent reads + parses to 2
_LOAD_SEM = threading.BoundedSemaphore(2)


# ---------------------------------------------------------------------------
# LRU cache for listing facts
# ---------------------------------------------------------------------------

class RunCache:
    """Thread-safe 1024-entry LRU cache for per-run listing facts.

    Keys are the combined signature of all three artifacts: the manifest's
    ``(size, mtime_ns, dev, ino)`` followed by the evidence and inspect
    signatures (each a 4-tuple, or the token ``"missing"``).
    Values are :class:`ListFacts`.
    """

    def __init__(self, maxsize: int = 1024) -> None:
        self._maxsize = maxsize
        self._cache: OrderedDict[tuple[Any, ...], ListFacts] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, sig: tuple[Any, ...]) -> ListFacts | None:
        with self._lock:
            if sig in self._cache:
                self._cache.move_to_end(sig)
                return self._cache[sig]
        return None

    def put(self, sig: tuple[Any, ...], facts: ListFacts) -> None:
        with self._lock:
            if sig in self._cache:
                self._cache.move_to_end(sig)
            else:
                self._cache[sig] = facts
                if len(self._cache) > self._maxsize:
                    self._cache.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


# ---------------------------------------------------------------------------
# Artifact state helpers
# ---------------------------------------------------------------------------

def _artifact_state_from_read(ar: ArtifactRead) -> ArtifactState:
    """Convert an ``ArtifactRead`` into an ``ArtifactState`` (pre-parse)."""
    if ar.fault is None:
        return ArtifactState(status=ArtifactStatus.OK.value, reason=None)
    status = STATUS_OF.get(ar.fault, ArtifactStatus.UNREADABLE)
    observed = ar.data  # None for read failures; bytes already dropped for too_large
    # For too_large, observed_size comes from the fault reason (or None)
    if ar.fault == ArtifactFault.TOO_LARGE:
        return ArtifactState(
            status=ArtifactStatus.TOO_LARGE.value,
            reason=ar.reason or "too_large",
            observed_size=None,  # size not stored here; files.py drops data
        )
    return ArtifactState(status=status.value, reason=ar.reason)


def _not_read_state() -> ArtifactState:
    """The not_read state assigned when the manifest is unusable."""
    return ArtifactState(status=ArtifactStatus.NOT_READ.value, reason="manifest_unusable")


def _manifest_unusable(state: ArtifactState) -> bool:
    """True when this manifest state means evidence/inspect must not be read."""
    return state.status in (
        ArtifactStatus.TOO_LARGE.value,
        ArtifactStatus.UNREADABLE.value,
        ArtifactStatus.INVALID.value,
        ArtifactStatus.MISSING.value,
    )


# ---------------------------------------------------------------------------
# Single-run loader
# ---------------------------------------------------------------------------

def _parse_manifest(ar: ArtifactRead, run_id: str) -> tuple[RunManifest | None, ArtifactState]:
    """Parse manifest bytes; return (manifest, state)."""
    if ar.fault is not None or ar.data is None:
        state = _artifact_state_from_read(ar)
        return None, state

    data = ar.data
    try:
        manifest = deserialize_manifest(data)
    except RecursionError:
        return None, ArtifactState(status=ArtifactStatus.INVALID.value, reason="manifest_invalid")
    except Exception:  # noqa: BLE001
        return None, ArtifactState(status=ArtifactStatus.INVALID.value, reason="manifest_invalid")

    # R-A: run_id mismatch -> manifest invalid
    if manifest.run_id != run_id:
        return None, ArtifactState(status=ArtifactStatus.INVALID.value, reason="run_id_mismatch")

    return manifest, ArtifactState(status=ArtifactStatus.OK.value, reason=None)


def _parse_evidence(
    ar: ArtifactRead, run_id: str
) -> tuple[ReadResult | None, RunSummary | None, ArtifactState]:
    """Parse evidence bytes; return (read_result, summary, state)."""
    if ar.fault is not None or ar.data is None:
        state = _artifact_state_from_read(ar)
        return None, None, state

    data = ar.data
    try:
        result = read_evidence(data, name=f"{run_id}.jsonl")
    except Exception:  # noqa: BLE001
        return None, None, ArtifactState(status=ArtifactStatus.INVALID.value, reason="parse_error")

    try:
        summary = summarize(result)
    except Exception:  # noqa: BLE001
        return result, None, ArtifactState(
            status=ArtifactStatus.INVALID.value, reason="evidence_summary_failed"
        )

    return result, summary, ArtifactState(status=ArtifactStatus.OK.value, reason=None)


def _parse_inspect(
    ar: ArtifactRead,
) -> tuple[Any, ArtifactState]:
    """Parse docker_inspect.json bytes; return (excerpt_or_None, state)."""
    if ar.fault is not None or ar.data is None:
        state = _artifact_state_from_read(ar)
        return None, state

    data = ar.data
    try:
        excerpt = extract_inspect_excerpt(data)
    except RecursionError:
        return None, ArtifactState(status=ArtifactStatus.INVALID.value, reason="inspect_rejected")
    except InspectError as exc:
        # Distinguish JSON parse failure (has ValueError cause) from shape rejection
        if exc.__cause__ is not None and isinstance(exc.__cause__, ValueError):
            reason = "inspect_invalid_json"
        else:
            reason = "inspect_rejected"
        return None, ArtifactState(status=ArtifactStatus.INVALID.value, reason=reason)
    except Exception:  # noqa: BLE001
        return None, ArtifactState(status=ArtifactStatus.INVALID.value, reason="inspect_rejected")

    return excerpt, ArtifactState(status=ArtifactStatus.OK.value, reason=None)


_READ_FAULT_CODES = (
    ArtifactStatus.MISSING.value,
    ArtifactStatus.UNREADABLE.value,
    ArtifactStatus.TOO_LARGE.value,
)


def _docker_inputs(
    ar: ArtifactRead, state: ArtifactState,
) -> tuple[bytes | None, str | None, str | None]:
    """(bytes, read-fault code, raw FinishedAt) to hand to the Docker state resolver.

    The bytes are passed whenever the read succeeded, so ``classify`` derives
    ``invalid_json`` versus ``rejected`` from ``InspectError.__cause__`` (never from
    the artifact reason text). The fault code is the artifact status of a failed
    read (``missing``, ``unreadable`` or ``too_large``). ``FinishedAt`` is extracted
    only from an inspect file that parsed.
    """
    if ar.fault is None and ar.data is not None:
        finished_at = _raw_finished_at(ar.data) if state.status == ArtifactStatus.OK.value else None
        return ar.data, None, finished_at
    fault = state.status if state.status in _READ_FAULT_CODES else None
    return None, fault, None


def load_detail(
    root: RunsRoot,
    run_id: str,
    io: ArtifactIO,
    clock_ns: Callable[[], int],
) -> RunRecord:
    """Load all artifacts for one run and return a fully populated :class:`RunRecord`.

    The detail path is NOT cached (each request re-reads from disk).
    """
    record = RunRecord(run_id=run_id)

    # Find the run directory
    entry = find_run(root, run_id, io)
    if entry is None:
        # Should not happen (caller checked); return minimal record
        record.manifest_fault = ArtifactState(status=ArtifactStatus.MISSING.value, reason="missing")
        record.evidence_fault = _not_read_state()
        record.inspect_fault = _not_read_state()
        return record

    with _LOAD_SEM:
        manifest_ar = read_artifact(root, entry, ArtifactName.MANIFEST, io)

    manifest, manifest_state = _parse_manifest(manifest_ar, run_id)
    record.manifest = manifest
    record.manifest_fault = manifest_state

    # not_read rule
    if _manifest_unusable(manifest_state):
        record.evidence_fault = _not_read_state()
        record.inspect_fault = _not_read_state()
        # Still compute classification from no manifest
        record.experiment_type = None
        record.verdict = "UNKNOWN"
        record.docker_view = resolve_docker_state_with_fault(None, None, run_id)
        record.sample_rows = []
        record.rss_stats_data = None
        record.last_persisted_row = None
        record.latest_capture_row = None
        record.offset_resolution = None
        record.chart_data = None
        record.viewer_warnings = _artifact_warnings([
            ("manifest", manifest_state),
            ("evidence", record.evidence_fault),
            ("inspect", record.inspect_fault),
        ])
        return record

    # Read evidence and inspect only when manifest is usable
    with _LOAD_SEM:
        evidence_ar = read_artifact(root, entry, ArtifactName.EVIDENCE, io)
        inspect_ar = read_artifact(root, entry, ArtifactName.INSPECT, io)

    result, summary, evidence_state = _parse_evidence(evidence_ar, run_id)
    record.read_result = result
    record.summary = summary
    record.evidence_fault = evidence_state

    inspect_excerpt, inspect_state = _parse_inspect(inspect_ar)
    record.inspect_excerpt = inspect_excerpt
    record.inspect_fault = inspect_state

    inspect_bytes_for_classify, raw_fault, raw_finished_at = _docker_inputs(inspect_ar, inspect_state)

    # Classification
    exp_type = classify_experiment_type(manifest.experiment if manifest else None)
    record.experiment_type = exp_type
    record.verdict = verdict_of(manifest.outcome if manifest else None)

    # Docker state
    record.docker_view = resolve_docker_state_with_fault(
        manifest,
        inspect_bytes_for_classify,
        run_id,
        raw_fault=raw_fault,
        raw_finished_at=raw_finished_at,
    )

    # Samples and stats
    if result is not None:
        rows = extract_samples(result)
        record.sample_rows = rows
        record.rss_stats_data = rss_stats(rows)
        record.last_persisted_row = last_persisted(rows)
        record.latest_capture_row = latest_capture(rows)
    else:
        record.sample_rows = []
        record.rss_stats_data = None
        record.last_persisted_row = None
        record.latest_capture_row = None

    # Clock offset resolution
    extra = manifest.extra if manifest and manifest.extra else {}
    res = resolve_offset(exp_type, extra)
    record.offset_resolution = res

    # Timeline/chart
    if result is not None and record.sample_rows:
        gaps = gap_index(result)
        mode, _correction_reason = axis_mode(exp_type, res)
        offset_ms_val = res.offset_ms if res else None

        # Sigkill sent time for the chart marker
        times = extra.get("times") if extra else None
        sigkill_ms: int | None = None
        if isinstance(times, dict):
            sigkill_ms = epoch_ms_utc(times.get("sigkill_sent_time"))
        # The SIGKILL x position joins the x-axis domain only when the marker is
        # drawn: Experiment B with a usable offset (corrected axis).
        if mode != "corrected" or offset_ms_val is None:
            sigkill_ms = None

        try:
            chart = build_chart(
                record.sample_rows,
                gaps,
                mode,
                offset_ms_val,
                cap=CHART_CAP,
                sigkill_x_ms=sigkill_ms,
            )
            record.chart_data = chart
        except Exception:  # noqa: BLE001
            record.chart_data = None
    else:
        record.chart_data = None

    # Viewer warnings
    warnings: list[dict[str, Any]] = []

    # record_count_mismatch warning
    if manifest and manifest.evidence is not None and result is not None:
        manifest_count = manifest.evidence.record_count
        actual_count = len(result.records)
        if manifest_count is not None and manifest_count != actual_count:
            warnings.append({
                "source": "viewer",
                "code": "record_count_mismatch",
                "text": (
                    f"manifest.evidence.record_count ({manifest_count}) "
                    f"does not match the number of valid records ({actual_count})"
                ),
            })

    warnings.extend(_artifact_warnings([
        ("manifest", manifest_state),
        ("evidence", evidence_state),
        ("inspect", inspect_state),
    ]))

    record.viewer_warnings = warnings
    return record


_ARTIFACT_WARN_STATUSES = {
    ArtifactStatus.MISSING.value,
    ArtifactStatus.UNREADABLE.value,
    ArtifactStatus.INVALID.value,
    ArtifactStatus.TOO_LARGE.value,
    ArtifactStatus.NOT_READ.value,
}


def _artifact_warnings(states: list[tuple[str, ArtifactState]]) -> list[dict[str, Any]]:
    """``artifact_<name>_<status>`` viewer warnings for every non-ok artifact state."""
    out: list[dict[str, Any]] = []
    for art_name, art_state in states:
        if art_state.status not in _ARTIFACT_WARN_STATUSES:
            continue
        code = f"artifact_{art_name}_{art_state.status}"
        if art_state.status == ArtifactStatus.NOT_READ.value:
            text = "not read (manifest unusable)"
        elif art_state.status == ArtifactStatus.MISSING.value:
            text = f"{art_name} artifact is missing"
        elif art_state.status == ArtifactStatus.UNREADABLE.value:
            text = f"{art_name} artifact is unreadable"
        elif art_state.status == ArtifactStatus.INVALID.value:
            text = f"{art_name} artifact is invalid"
        elif art_state.status == ArtifactStatus.TOO_LARGE.value:
            text = f"{art_name} artifact is too large"
        else:
            text = f"{art_name} artifact status: {art_state.status}"
        out.append({"source": "viewer", "code": code, "text": text})
    return out


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def _build_list_facts(
    root: RunsRoot,
    entry: RunEntry,
    io: ArtifactIO,
    clock_ns: Callable[[], int],
    cache: RunCache | None,
) -> ListFacts:
    """Build :class:`ListFacts` for one run, using the cache when possible."""
    run_id = entry.run_id

    # Signatures of all three artifacts, taken BEFORE any read (lstat only).
    # The other two artifacts are only statted when the manifest itself is
    # statable; a manifest that fails the containment checks is never cached.
    names = (ArtifactName.MANIFEST, ArtifactName.EVIDENCE, ArtifactName.INSPECT)
    key: tuple[Any, ...] | None = None
    pre_sigs: tuple[Any, ...] = ()
    racy = True
    if cache is not None:
        manifest_sig = artifact_signature(root, entry, ArtifactName.MANIFEST, io)
        if manifest_sig is not None:
            pre_sigs = (manifest_sig,) + tuple(artifact_signature(root, entry, n, io) for n in names[1:])
            key = manifest_sig + tuple(s if s is not None else "missing" for s in pre_sigs[1:])
            # Racily clean: the newest mtime_ns of any artifact decides.
            newest = max(s[1] for s in pre_sigs if s is not None)
            racy = clock_ns() - newest < _RACILY_CLEAN_NS
            if not racy:
                cached = cache.get(key)
                if cached is not None:
                    return cached

    def _store(facts: ListFacts) -> None:
        """Store only when not racily clean and no signature changed during the load."""
        if cache is None or key is None or racy:
            return
        post_sigs = tuple(artifact_signature(root, entry, n, io) for n in names)
        if post_sigs != pre_sigs:
            return
        cache.put(key, facts)

    # Read the manifest (with semaphore)
    with _LOAD_SEM:
        manifest_ar = read_artifact(root, entry, ArtifactName.MANIFEST, io)

    # Parse the manifest
    manifest, manifest_state = _parse_manifest(manifest_ar, run_id)

    if _manifest_unusable(manifest_state):
        # Determine the specific reason code for the UNKNOWN listing
        reason = manifest_state.reason or "manifest_invalid"
        # Map status to reason code
        if manifest_state.status == ArtifactStatus.MISSING.value:
            if manifest_state.reason in ("symlink_or_reparse_point", "unsafe_path", "identity_changed", "outside_runs_directory"):
                reason = "manifest_unsafe_path"
            elif manifest_state.reason == "not_regular_file":
                reason = "manifest_not_regular_file"
            else:
                reason = "manifest_missing"
        elif manifest_state.status == ArtifactStatus.UNREADABLE.value:
            reason = "manifest_unreadable"
        elif manifest_state.status == ArtifactStatus.TOO_LARGE.value:
            reason = "manifest_too_large"
        elif manifest_state.status == ArtifactStatus.INVALID.value:
            reason = manifest_state.reason or "manifest_invalid"

        facts = ListFacts(
            run_id=run_id,
            verdict="UNKNOWN",
            reason=reason,
        )
        _store(facts)
        return facts

    # Manifest is usable - read evidence for sample count
    with _LOAD_SEM:
        evidence_ar = read_artifact(root, entry, ArtifactName.EVIDENCE, io)

    result, summary, evidence_state = _parse_evidence(evidence_ar, run_id)

    exp_type = classify_experiment_type(manifest.experiment)
    extra = manifest.extra if manifest.extra else {}

    # Docker state (read inspect for effective values)
    with _LOAD_SEM:
        inspect_ar = read_artifact(root, entry, ArtifactName.INSPECT, io)

    inspect_excerpt, inspect_state = _parse_inspect(inspect_ar)
    inspect_bytes_for_classify, raw_fault, raw_finished_at = _docker_inputs(inspect_ar, inspect_state)

    docker_view = resolve_docker_state_with_fault(
        manifest,
        inspect_bytes_for_classify,
        run_id,
        raw_fault=raw_fault,
        raw_finished_at=raw_finished_at,
    )

    # Effective exit_code and oom_killed
    exit_code: int | None = None
    oom_killed: bool | None = None
    if docker_view.effective is not None:
        exit_code = docker_view.effective.exit_code
        oom_killed = docker_view.effective.oom_killed

    # Docker state discrepancy
    discrepancy: bool | None = None
    if docker_view.source not in ("unavailable",) and inspect_state.status == ArtifactStatus.OK.value:
        discrepancy = len(docker_view.discrepancies) > 0

    # Sample count and collector survival from checks
    sample_count: int | None = None
    if summary is not None:
        sample_count = summary.sample_count

    collector_survival: bool | None = None
    checks_raw = extra.get("checks")
    if isinstance(checks_raw, list):
        for check in checks_raw:
            if isinstance(check, dict) and check.get("name") == "collector_survival":
                val = check.get("passed")
                if isinstance(val, bool):
                    collector_survival = val
                break

    # start_time and its epoch ms
    start_time = manifest.start_time if manifest else None
    start_time_ms: int | None = None
    if start_time is not None:
        start_time_ms = epoch_ms_utc(start_time)

    facts = ListFacts(
        run_id=run_id,
        experiment=manifest.experiment if manifest else None,
        experiment_type=exp_type,
        verdict=verdict_of(manifest.outcome if manifest else None),
        start_time=start_time,
        start_time_ms=start_time_ms,
        exit_code=exit_code,
        oom_killed=oom_killed,
        docker_state_source=docker_view.source,
        docker_state_discrepancy=discrepancy,
        sample_count=sample_count,
        collector_survival=collector_survival,
        reason=None,
    )

    # Store only when the signatures are unchanged after parsing and not racily clean
    _store(facts)

    return facts


def load_listing(
    root: RunsRoot,
    io: ArtifactIO,
    clock_ns: Callable[[], int],
    cache: RunCache | None = None,
) -> dict[str, Any]:
    """Build the listing payload for ``GET /api/runs``.

    Returns a dict with ``total_found``, ``listed``, ``truncated``,
    ``max_listed``, and ``facts`` (a list of :class:`ListFacts`).
    """
    entries = list_run_entries(root, io)
    total_found = len(entries)
    truncated = total_found > MAX_LISTED_RUNS
    to_process = entries[:MAX_LISTED_RUNS]

    facts_list: list[ListFacts] = []
    for entry in to_process:
        try:
            facts = _build_list_facts(root, entry, io, clock_ns, cache)
        except Exception:  # noqa: BLE001
            facts = ListFacts(
                run_id=entry.run_id,
                verdict="UNKNOWN",
                reason="internal_error",
            )
        facts_list.append(facts)

    # Sort in Run_Order:
    # Group 1: runs with a valid start_time (start_time_ms is not None), sorted by
    #   (start_time_ms DESC, run_id DESC)
    # Group 2: runs without a valid start_time, sorted by run_id DESC
    valid_group: list[ListFacts] = []
    invalid_group: list[ListFacts] = []
    for f in facts_list:
        if f.start_time_ms is not None:
            valid_group.append(f)
        else:
            invalid_group.append(f)

    valid_group.sort(key=lambda f: (f.start_time_ms, f.run_id), reverse=True)
    invalid_group.sort(key=lambda f: f.run_id, reverse=True)

    ordered = valid_group + invalid_group

    return {
        "total_found": total_found,
        "listed": len(ordered),
        "truncated": truncated,
        "max_listed": MAX_LISTED_RUNS,
        "facts": ordered,
        "runs_dir_label": root.label,
    }
