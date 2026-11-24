"""Classification, verdict and Docker state resolution for the Coroner Viewer.

Implements:
  - ``experiment_type``: maps the manifest experiment string to A/B/C or None.
  - ``verdict_of``: maps the Stored_Outcome to PASS/FAIL/UNKNOWN.
  - ``resolve_docker_state``: compares Manifest_Derived_State and Raw_Inspect,
    produces the ``DockerView`` with source code, discrepancies, effective values
    and per-field comparison results.

Import constraints: stdlib only plus coroner.runner.manifest (InspectError,
RunManifest) and coroner.viewer.timefmt (parse_docker_time, epoch_ms_docker,
epoch_ms_utc).  Never imports coroner.runner.experiments.base,
coroner.runner.evaluate, or any Docker client library.  OOMKilled is never
inferred from RSS, memory limit, or ExitCode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from coroner.runner.manifest import InspectError, RunManifest
from coroner.viewer.timefmt import epoch_ms_docker, epoch_ms_utc

__all__ = [
    "DockerManifestBlock",
    "DockerRawBlock",
    "DockerEffective",
    "DockerFieldResult",
    "DockerView",
    "experiment_type",
    "verdict_of",
    "resolve_docker_state",
]

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DockerManifestBlock:
    """Values sourced from the manifest ``container_state``."""
    present: bool
    container_id: str | None = None          # container_state.container_id
    status: str | None = None                # container_state.status
    exit_code: int | None = None             # container_state.exit_code
    oom_killed: bool | None = None           # container_state.oom_killed
    memory_limit_bytes: int | None = None    # container_state.host_config_memory_bytes
    inspect_time: str | None = None          # container_state.inspect_time
    finished_at: str | None = None           # top-level container_finished_at


@dataclass(frozen=True)
class DockerRawBlock:
    """Values sourced from docker_inspect.json (InspectExcerpt)."""
    container_id: str | None = None          # InspectExcerpt.id (64-hex normalised to lowercase)
    status: str | None = None                # State.Status lowercased
    exit_code: int | None = None             # State.ExitCode
    oom_killed: bool | None = None           # State.OOMKilled (exact bool)
    memory_limit_bytes: int | None = None    # HostConfig.Memory
    finished_at: str | None = None           # State.FinishedAt (original; None for zero-date/unparseable)


@dataclass(frozen=True)
class DockerEffective:
    """Per-field effective value: manifest first, then raw, never a default."""
    container_id: str | None = None
    status: str | None = None
    exit_code: int | None = None
    oom_killed: bool | None = None
    memory_limit_bytes: int | None = None
    memory_limit_kind: str | None = None     # "none_configured" | "bytes" | None
    finished_at: str | None = None


@dataclass(frozen=True)
class DockerFieldResult:
    """Comparison result for one compared field."""
    field: str
    manifest_value: Any
    raw_value: Any
    result: str   # "equal" | "discrepancy" | "not_compared_manifest_missing" |
                  #  "not_compared_raw_missing" | "not_recorded_both"


@dataclass
class DockerView:
    """Full Docker state resolution output."""
    source: str                                     # one of the five source codes
    raw_unavailable_reason: str | None = None       # "missing"|"unreadable"|"invalid_json"|"rejected"|"too_large"|None
    manifest: DockerManifestBlock | None = None
    raw: DockerRawBlock | None = None
    effective: DockerEffective | None = None
    fields: list[DockerFieldResult] = field(default_factory=list)
    discrepancies: list[DockerFieldResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def experiment_type(experiment: object) -> str | None:
    """Return "A", "B", or "C" when *experiment* is a string equal to one of
    those letters (case-insensitive).  Returns ``None`` for any other value,
    including non-strings, empty string and strings that are not A/B/C.

    The type is **never** inferred from ExitCode, OOMKilled or the run id.
    """
    if not isinstance(experiment, str):
        return None
    up = experiment.upper()
    if up in ("A", "B", "C"):
        return up
    return None


def verdict_of(outcome: object) -> str:
    """Map the Stored_Outcome to the Viewer Verdict.

    ``"passed"`` → ``"PASS"``
    ``"failed"`` → ``"FAIL"``
    anything else (including ``None``, ``"aborted"``, or an unusable manifest)
    → ``"UNKNOWN"``
    """
    if outcome == "passed":
        return "PASS"
    if outcome == "failed":
        return "FAIL"
    return "UNKNOWN"


def resolve_docker_state(
    manifest_data: RunManifest | None,
    inspect_bytes: bytes | str | None,
    run_id: str,
    *,
    raw_finished_at: str | None = None,
) -> DockerView:
    """Resolve the Docker state from the manifest and raw inspect sources.

    Parameters
    ----------
    manifest_data:
        The parsed ``RunManifest``, or ``None`` when the manifest is unusable.
    inspect_bytes:
        The raw bytes/str of ``docker_inspect.json``, or ``None`` / a fault code.
        Pass ``None`` for a missing/unreadable/too_large inspect artifact.
        Actual bytes are parsed here; an ``ArtifactFault`` reason string must
        be passed instead (see *raw_fault* argument below).
    run_id:
        The run directory name used to validate ``manifest.run_id``.

    Returns a :class:`DockerView`.
    """
    # This signature is called with (manifest_data, inspect_bytes, run_id).
    # inspect_bytes can be None (missing/unreadable), or actual bytes/str to parse.
    return _resolve(manifest_data, inspect_bytes, run_id, raw_finished_at=raw_finished_at)


def resolve_docker_state_with_fault(
    manifest_data: RunManifest | None,
    inspect_bytes: bytes | str | None,
    run_id: str,
    *,
    raw_fault: str | None = None,
    raw_finished_at: str | None = None,
) -> DockerView:
    """Extended resolve that accepts a pre-computed raw_fault code.

    ``raw_fault`` is one of ``"missing"``, ``"unreadable"``, ``"too_large"``
    when ``inspect_bytes is None`` for that reason.  When ``inspect_bytes``
    is not ``None``, ``raw_fault`` is ignored.
    """
    return _resolve(
        manifest_data, inspect_bytes, run_id,
        raw_fault=raw_fault, raw_finished_at=raw_finished_at,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve(
    manifest_data: RunManifest | None,
    inspect_bytes: bytes | str | None,
    run_id: str,
    *,
    raw_fault: str | None = None,
    raw_finished_at: str | None = None,
) -> DockerView:
    """Core implementation shared by the two public entry points."""
    # 1. Build the manifest block
    mblock = _build_manifest_block(manifest_data)

    # 2. Parse the raw inspect
    raw_result = _parse_raw(inspect_bytes, raw_fault, raw_finished_at)
    rblock: DockerRawBlock | None = raw_result["block"]
    raw_unavailable_reason: str | None = raw_result["unavailable_reason"]

    # 3. Determine source code
    has_manifest_state = mblock is not None and mblock.present
    has_raw = rblock is not None

    if has_manifest_state and has_raw:
        # compare fields; source depends on whether there are discrepancies
        field_results = _compare_fields(mblock, rblock)
        disc = [f for f in field_results if f.result == "discrepancy"]
        source = "inspect_and_manifest_agree" if not disc else "inspect_and_manifest_disagree"
    elif has_manifest_state and not has_raw:
        source = "manifest_only"
        field_results = _compare_fields(mblock, None)
        disc = []
    elif not has_manifest_state and has_raw:
        source = "raw_only"
        field_results = _compare_fields(None, rblock)
        disc = []
    else:
        source = "unavailable"
        field_results = []
        disc = []

    # 4. Effective values
    effective = _build_effective(mblock, rblock)

    return DockerView(
        source=source,
        raw_unavailable_reason=raw_unavailable_reason,
        manifest=mblock,
        raw=rblock,
        effective=effective,
        fields=field_results,
        discrepancies=disc,
    )


def _build_manifest_block(manifest_data: RunManifest | None) -> DockerManifestBlock | None:
    """Build a :class:`DockerManifestBlock` from the manifest, or ``None``."""
    if manifest_data is None:
        return DockerManifestBlock(present=False)

    cs = manifest_data.container_state
    if cs is None:
        # manifest present but no container_state; we still want finished_at if available
        finished_at_raw = manifest_data.extra.get("container_finished_at") if manifest_data.extra else None
        finished_at = _manifest_finished_at(finished_at_raw)
        return DockerManifestBlock(present=False, finished_at=finished_at)

    # container_state is present
    finished_at_raw = manifest_data.extra.get("container_finished_at") if manifest_data.extra else None
    finished_at = _manifest_finished_at(finished_at_raw)

    return DockerManifestBlock(
        present=True,
        container_id=cs.container_id if isinstance(cs.container_id, str) else None,
        status=cs.status if isinstance(cs.status, str) else None,
        exit_code=cs.exit_code if (isinstance(cs.exit_code, int) and not isinstance(cs.exit_code, bool)) else None,
        oom_killed=cs.oom_killed if isinstance(cs.oom_killed, bool) else None,
        memory_limit_bytes=(
            cs.host_config_memory_bytes
            if (isinstance(cs.host_config_memory_bytes, int) and not isinstance(cs.host_config_memory_bytes, bool))
            else None
        ),
        inspect_time=cs.inspect_time if isinstance(cs.inspect_time, str) else None,
        finished_at=finished_at,
    )


def _manifest_finished_at(raw: object) -> str | None:
    """Return the manifest container_finished_at string when it parses; None otherwise."""
    if not isinstance(raw, str):
        return None
    # Must be parseable by parse_utc_ms (strict ms format)
    try:
        from coroner.common.timeutil import parse_utc_ms
        parse_utc_ms(raw)
        return raw
    except (ValueError, TypeError):
        return None


def _parse_raw(
    inspect_bytes: bytes | str | None,
    raw_fault: str | None,
    raw_finished_at: str | None = None,
) -> dict[str, Any]:
    """Parse raw inspect bytes, returning a dict with 'block' and 'unavailable_reason'."""
    if inspect_bytes is None:
        # Determine reason from pre-supplied fault code
        reason = raw_fault if raw_fault in ("missing", "unreadable", "too_large") else "missing"
        return {"block": None, "unavailable_reason": reason}

    # Attempt to parse
    try:
        from coroner.runner.manifest import extract_inspect_excerpt
        excerpt = extract_inspect_excerpt(inspect_bytes)
    except InspectError as exc:
        # Distinguish invalid_json (cause is a ValueError from json.loads) from rejected
        # (shape problems, which have no ValueError cause set by json.loads)
        if isinstance(exc.__cause__, ValueError):
            reason = "invalid_json"
        else:
            reason = "rejected"
        return {"block": None, "unavailable_reason": reason}
    except RecursionError:
        # Valid-looking JSON nested too deeply to decode: the content is rejected.
        return {"block": None, "unavailable_reason": "rejected"}
    except Exception:
        return {"block": None, "unavailable_reason": "unreadable"}

    # Normalise values per design spec
    # container_id: 64-hex normalised to lowercase
    cid = excerpt.id.lower() if isinstance(excerpt.id, str) else None

    # status: lowercased
    status = excerpt.status.lower() if isinstance(excerpt.status, str) else None

    # exit_code: int (InspectExcerpt guarantees it is int and not bool)
    exit_code = excerpt.exit_code

    # oom_killed: exact bool (InspectExcerpt guarantees bool)
    oom_killed = excerpt.oom_killed

    # memory: int (InspectExcerpt guarantees int and not bool)
    memory = excerpt.memory

    # finished_at: InspectExcerpt does not carry FinishedAt; loader.py extracts it
    # from the bounded bytes and supplies it. Zero/unparseable values arrive as None.
    finished_at = raw_finished_at if epoch_ms_docker(raw_finished_at) is not None else None

    block = DockerRawBlock(
        container_id=cid,
        status=status,
        exit_code=exit_code,
        oom_killed=oom_killed,
        memory_limit_bytes=memory,
        finished_at=finished_at,
    )
    return {"block": block, "unavailable_reason": None}


def _compare_fields(
    mblock: DockerManifestBlock | None,
    rblock: DockerRawBlock | None,
) -> list[DockerFieldResult]:
    """Build per-field comparison results for all six compared fields."""
    results: list[DockerFieldResult] = []

    # Helper to add one field comparison
    def _cmp(fname: str, mval: object, rval: object, *, eq: object = None) -> None:
        m_present = mblock is not None and mblock.present
        r_present = rblock is not None

        if mval is None and rval is None:
            results.append(DockerFieldResult(
                field=fname, manifest_value=None, raw_value=None,
                result="not_recorded_both",
            ))
        elif mval is None and not m_present:
            results.append(DockerFieldResult(
                field=fname, manifest_value=None, raw_value=rval,
                result="not_compared_manifest_missing",
            ))
        elif rval is None and not r_present:
            results.append(DockerFieldResult(
                field=fname, manifest_value=mval, raw_value=None,
                result="not_compared_raw_missing",
            ))
        elif mval is None:
            # manifest present but field is null
            results.append(DockerFieldResult(
                field=fname, manifest_value=None, raw_value=rval,
                result="not_compared_manifest_missing",
            ))
        elif rval is None:
            # raw present but field is null
            results.append(DockerFieldResult(
                field=fname, manifest_value=mval, raw_value=None,
                result="not_compared_raw_missing",
            ))
        else:
            # Both non-null: compare
            are_equal = (mval == rval) if eq is None else eq
            results.append(DockerFieldResult(
                field=fname, manifest_value=mval, raw_value=rval,
                result="equal" if are_equal else "discrepancy",
            ))

    m_present = mblock is not None and mblock.present
    r_present = rblock is not None

    # container_id: exact string comparison
    m_cid = mblock.container_id if m_present else None
    r_cid = rblock.container_id if r_present else None
    _cmp("container_id", m_cid, r_cid)

    # status: exact string comparison
    m_status = mblock.status if m_present else None
    r_status = rblock.status if r_present else None
    _cmp("status", m_status, r_status)

    # exit_code: int equality
    m_exit = mblock.exit_code if m_present else None
    r_exit = rblock.exit_code if r_present else None
    _cmp("exit_code", m_exit, r_exit)

    # oom_killed: bool equality
    m_oom = mblock.oom_killed if m_present else None
    r_oom = rblock.oom_killed if r_present else None
    _cmp("oom_killed", m_oom, r_oom)

    # memory_limit_bytes: int equality (0 is a valid value)
    m_mem = mblock.memory_limit_bytes if m_present else None
    r_mem = rblock.memory_limit_bytes if r_present else None
    _cmp("memory_limit_bytes", m_mem, r_mem)

    # finished_at: compare at ms truncation precision
    # Both sides use epoch ms after parsing; "not recorded" for zero-date or unparseable
    m_fat_raw = mblock.finished_at if mblock is not None else None
    r_fat_raw = rblock.finished_at if rblock is not None else None

    m_fat_ms = epoch_ms_utc(m_fat_raw) if m_fat_raw is not None else None
    r_fat_ms = epoch_ms_docker(r_fat_raw) if r_fat_raw is not None else None

    if m_fat_ms is None and r_fat_ms is None:
        results.append(DockerFieldResult(
            field="finished_at", manifest_value=None, raw_value=None,
            result="not_recorded_both",
        ))
    elif m_fat_ms is None:
        results.append(DockerFieldResult(
            field="finished_at", manifest_value=None, raw_value=r_fat_raw,
            result="not_compared_manifest_missing",
        ))
    elif r_fat_ms is None:
        results.append(DockerFieldResult(
            field="finished_at", manifest_value=m_fat_raw, raw_value=None,
            result="not_compared_raw_missing",
        ))
    else:
        # Both present: compare at ms truncation (ms values are already truncated)
        are_equal = (m_fat_ms == r_fat_ms)
        results.append(DockerFieldResult(
            field="finished_at",
            manifest_value=m_fat_raw,
            raw_value=r_fat_raw,
            result="equal" if are_equal else "discrepancy",
        ))

    return results


def _build_effective(
    mblock: DockerManifestBlock | None,
    rblock: DockerRawBlock | None,
) -> DockerEffective:
    """Build the effective value block: manifest first, then raw, never a default."""
    m_present = mblock is not None and mblock.present
    r_present = rblock is not None

    def _first(mval: object, rval: object) -> Any:
        return mval if mval is not None else rval

    m_cid = mblock.container_id if m_present else None
    r_cid = rblock.container_id if r_present else None
    eff_cid = _first(m_cid, r_cid)

    m_status = mblock.status if m_present else None
    r_status = rblock.status if r_present else None
    eff_status = _first(m_status, r_status)

    m_exit = mblock.exit_code if m_present else None
    r_exit = rblock.exit_code if r_present else None
    eff_exit = _first(m_exit, r_exit)

    m_oom = mblock.oom_killed if m_present else None
    r_oom = rblock.oom_killed if r_present else None
    eff_oom = _first(m_oom, r_oom)

    m_mem = mblock.memory_limit_bytes if m_present else None
    r_mem = rblock.memory_limit_bytes if r_present else None
    eff_mem = _first(m_mem, r_mem)

    # memory_limit_kind
    if eff_mem is None:
        mem_kind = None
    elif eff_mem == 0:
        mem_kind = "none_configured"
    else:
        mem_kind = "bytes"

    # finished_at effective: manifest container_finished_at first, then raw
    m_fat = mblock.finished_at if mblock is not None else None
    r_fat = rblock.finished_at if rblock is not None else None
    eff_fat = _first(m_fat, r_fat)

    return DockerEffective(
        container_id=eff_cid,
        status=eff_status,
        exit_code=eff_exit,
        oom_killed=eff_oom,
        memory_limit_bytes=eff_mem,
        memory_limit_kind=mem_kind,
        finished_at=eff_fat,
    )
