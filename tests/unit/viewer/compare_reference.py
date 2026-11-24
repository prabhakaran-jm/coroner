"""Python reference model of the /compare statements (Requirement 8).

Written from Requirement 8 and the design table "Statements", not from any
JavaScript. It uses recorded values only. It never infers one run from another
and never infers a cause from ExitCode alone.

Slot input (one object per slot ``a``, ``b``, ``c``), the facts the compare page
takes from ``GET /api/runs`` and ``GET /api/runs/{run_id}``:

    state              "loaded" | "none" | "unavailable" | "error"
                       none: no saved run exists for the slot's type
                       unavailable: a run id was supplied and the API answered 404
                       error: another failure (``http_status``)
    run_id             string or null
    experiment_type    "A" | "B" | "C" (loaded runs only)
    exit_code          docker_state.effective.exit_code (int or null)
    oom_killed         docker_state.effective.oom_killed (bool or null)
    sample_count       evidence_summary.sample_count (int or null)
    discrepant_fields  names from docker_state.discrepancies
    http_status        int (state "error" only)
    docker_state_source informational only; never used by the logic
"""

from __future__ import annotations

from typing import Any

EXECUTES_JAVASCRIPT = False

SLOT_TYPE = {"a": "A", "b": "B", "c": "C"}
HEADLINE_TEXT = "The same exit code does not always mean the same failure"
SHARED_EXIT_CODE = 137


def _js_bool(value: bool) -> str:
    return "true" if value else "false"


def _slot_problem(name: str, slot: dict[str, Any]) -> str | None:
    """Reason why a slot cannot take part in any statement, or None."""
    label = name.upper()
    state = slot.get("state")
    if state == "none":
        return f"slot {label}: no saved run for Experiment {label}"
    if state == "unavailable":
        return f"slot {label}: no saved run found for run id {slot.get('run_id')}"
    if state == "error":
        return (
            f"slot {label}: run {slot.get('run_id')} could not be loaded "
            f"(HTTP {slot.get('http_status')})"
        )
    actual = slot.get("experiment_type")
    if actual != SLOT_TYPE[name]:
        return (
            f"slot {label}: run {slot.get('run_id')} is Experiment {actual}, "
            f"expected Experiment {label}"
        )
    return None


def _value_problem(name: str, slot: dict[str, Any], field: str) -> str | None:
    """Reason why one field of an included slot cannot be used, or None."""
    label = name.upper()
    shown = {"exit_code": "ExitCode", "oom_killed": "OOMKilled"}[field]
    if field in slot.get("discrepant_fields", []):
        return f"slot {label}: Docker state sources disagree ({shown})"
    if slot.get(field) is None:
        return f"slot {label}: {shown} not recorded"
    return None


def _reasons(
    slots: dict[str, dict[str, Any]], names: tuple[str, ...], fields: tuple[str, ...]
) -> list[str]:
    out: list[str] = []
    for name in names:
        slot = slots[name]
        problem = _slot_problem(name, slot)
        if problem is not None:
            out.append(problem)
            continue
        for field in fields:
            reason = _value_problem(name, slot, field)
            if reason is not None:
                out.append(reason)
    return out


def evaluate_comparison(slots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    statements: list[dict[str, str]] = []
    omitted: dict[str, list[str]] = {}
    headline: str | None = None

    def exit_of(name: str) -> int:
        return slots[name]["exit_code"]

    def oom_of(name: str) -> bool:
        return slots[name]["oom_killed"]

    # S1: A differs from B and C by exit code.
    reasons = _reasons(slots, ("a", "b", "c"), ("exit_code",))
    if reasons:
        omitted["S1"] = reasons
    elif exit_of("a") != exit_of("b") and exit_of("a") != exit_of("c"):
        statements.append(
            {
                "id": "S1",
                "text": (
                    "Experiment A differs from Experiment B and Experiment C by exit code "
                    f"(A={exit_of('a')}, B={exit_of('b')}, C={exit_of('c')})"
                ),
            }
        )

    # S2: B and C share ExitCode 137.
    exit_reasons = _reasons(slots, ("b", "c"), ("exit_code",))
    if exit_reasons:
        omitted["S2"] = exit_reasons
        shared = False
    else:
        shared = exit_of("b") == SHARED_EXIT_CODE and exit_of("c") == SHARED_EXIT_CODE
        if shared:
            statements.append(
                {
                    "id": "S2",
                    "text": "Experiment B and Experiment C share ExitCode 137",
                }
            )

    # S3 and the headline: only when B and C share 137 and OOMKilled differs.
    # When the exit codes are usable and not both 137, S3 does not apply and
    # nothing is "omitted". Otherwise every missing value is named.
    if exit_reasons or shared:
        s3_reasons = _reasons(slots, ("b", "c"), ("exit_code", "oom_killed"))
        if s3_reasons:
            omitted["S3"] = s3_reasons
            omitted["headline"] = list(s3_reasons)
        elif oom_of("b") != oom_of("c"):
            statements.append(
                {
                    "id": "S3",
                    "text": (
                        "OOMKilled distinguishes Experiment B "
                        f"({_js_bool(oom_of('b'))}) from Experiment C ({_js_bool(oom_of('c'))})"
                    ),
                }
            )
            headline = HEADLINE_TEXT

    # S4: evidence survived all three deaths.
    s4_reasons: list[str] = []
    for name in ("a", "b", "c"):
        slot = slots[name]
        problem = _slot_problem(name, slot)
        if problem is not None:
            s4_reasons.append(problem)
        elif slot.get("sample_count") is None:
            s4_reasons.append(f"slot {name.upper()}: Sample count not recorded")
        elif slot["sample_count"] == 0:
            s4_reasons.append(f"slot {name.upper()}: no preserved Samples")
    if s4_reasons:
        omitted["S4"] = s4_reasons
    else:
        counts = ", ".join(f"{n.upper()}={slots[n]['sample_count']}" for n in ("a", "b", "c"))
        statements.append(
            {
                "id": "S4",
                "text": (
                    "Evidence survived all three deaths in the recorded runs "
                    f"(Samples: {counts})"
                ),
            }
        )

    statements.sort(key=lambda s: s["id"])
    return {"statements": statements, "headline": headline, "omitted": omitted}
