"""Python layer of the /compare logic verification (Requirement 8, Requirement 11 criterion 32).

This file executes NO JavaScript. It checks the golden vectors against the Python
reference model only. The JavaScript module is verified by the Node vector test
(task 4.3) and by the manual real-browser smoke test, never by this file.
"""

from __future__ import annotations

import ast
import copy
import itertools
import json
from pathlib import Path

import pytest

from tests.unit.viewer import compare_reference as ref

VECTORS_PATH = Path(__file__).with_name("compare_vectors.json")
HEADLINE = "The same exit code does not always mean the same failure"


def _load() -> list[dict]:
    data = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    return data["vectors"]


VECTORS = _load()
BY_ID = {v["id"]: v for v in VECTORS}


@pytest.mark.parametrize("vector", VECTORS, ids=[v["id"] for v in VECTORS])
def test_reference_model_agrees_with_vector(vector):
    result = ref.evaluate_comparison(copy.deepcopy(vector["slots"]))
    assert result == vector["expected"]


def test_vector_ids_are_unique_and_slots_complete():
    ids = [v["id"] for v in VECTORS]
    assert len(ids) == len(set(ids))
    for v in VECTORS:
        assert set(v["slots"]) == {"a", "b", "c"}


def test_required_scenarios_are_covered():
    required = {
        "headline_recorded_abc",
        "same_oom_false",
        "same_oom_true",
        "non_137_exit_codes",
        "exit_137_for_b_only",
        "a_run_missing",
        "missing_run_c",
        "unknown_run_id_in_slot_b",
        "type_mismatch_b_run_in_slot_a",
        "type_mismatch_c_run_in_slot_b",
        "oom_killed_missing_for_c",
        "exit_code_missing_for_b",
        "discrepant_exit_code_in_c",
        "discrepant_oom_killed_in_b",
        "docker_state_unavailable_for_all",
        "docker_state_from_one_source_only",
        "zero_samples_in_b",
        "a_run_exit_code_missing",
    }
    assert required <= set(BY_ID)


def test_this_layer_executes_no_javascript(record_property):
    record_property("executes_javascript", False)
    assert ref.EXECUTES_JAVASCRIPT is False
    for path in (Path(__file__), Path(ref.__file__)):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & {"subprocess", "shutil", "os"}, path.name


def test_statements_for_b_137_false_and_c_137_true_differ():
    vector = BY_ID["headline_recorded_abc"]
    assert vector["slots"]["b"]["exit_code"] == vector["slots"]["c"]["exit_code"] == 137
    assert vector["slots"]["b"]["oom_killed"] is False
    assert vector["slots"]["c"]["oom_killed"] is True
    texts = {s["id"]: s["text"] for s in vector["expected"]["statements"]}
    assert "(false)" in texts["S3"] and "(true)" in texts["S3"]
    assert texts["S3"].index("(false)") < texts["S3"].index("(true)")
    result = ref.evaluate_comparison(copy.deepcopy(vector["slots"]))
    assert result["headline"] == HEADLINE


def test_headline_appears_only_in_the_headline_vectors():
    with_headline = {v["id"] for v in VECTORS if v["expected"]["headline"] is not None}
    for v in VECTORS:
        b, c = v["slots"]["b"], v["slots"]["c"]
        usable = (
            b["state"] == "loaded"
            and c["state"] == "loaded"
            and b["experiment_type"] == "B"
            and c["experiment_type"] == "C"
            and b["exit_code"] == 137
            and c["exit_code"] == 137
            and b["oom_killed"] is not None
            and c["oom_killed"] is not None
            and b["oom_killed"] != c["oom_killed"]
            and not {"exit_code", "oom_killed"} & set(b["discrepant_fields"])
            and not {"exit_code", "oom_killed"} & set(c["discrepant_fields"])
        )
        assert (v["id"] in with_headline) == usable, v["id"]
        if v["expected"]["headline"] is not None:
            assert v["expected"]["headline"] == HEADLINE


def _loaded(kind, exit_code, oom, samples=5, discrepant=()):
    return {
        "state": "loaded",
        "run_id": f"run-{kind.lower()}",
        "experiment_type": kind,
        "exit_code": exit_code,
        "oom_killed": oom,
        "sample_count": samples,
        "discrepant_fields": list(discrepant),
    }


def test_headline_condition_over_a_small_exhaustive_grid():
    """Independent re-statement of Requirement 8 criterion 8 over a small grid."""
    exits = (None, 1, 137)
    ooms = (None, False, True)
    discs = ((), ("exit_code",), ("oom_killed",), ("memory_limit_bytes",))
    for eb, ec, ob, oc, db, dc in itertools.product(exits, exits, ooms, ooms, discs, discs):
        slots = {
            "a": _loaded("A", 1, False),
            "b": _loaded("B", eb, ob, discrepant=db),
            "c": _loaded("C", ec, oc, discrepant=dc),
        }
        got = ref.evaluate_comparison(slots)
        blocked = {"exit_code", "oom_killed"}
        expect = (
            eb == 137
            and ec == 137
            and ob is not None
            and oc is not None
            and ob != oc
            and not blocked & set(db)
            and not blocked & set(dc)
        )
        assert (got["headline"] == HEADLINE) == expect, (eb, ec, ob, oc, db, dc)
        ids = {s["id"] for s in got["statements"]}
        assert ("S3" in ids) == expect
        # ExitCode alone never yields S3 or the headline.
        if ob is None or oc is None:
            assert "S3" not in ids and got["headline"] is None


def test_b_and_c_values_are_never_borrowed_from_each_other():
    slots = {
        "a": _loaded("A", 1, False),
        "b": _loaded("B", 137, None),
        "c": _loaded("C", 137, True),
    }
    got = ref.evaluate_comparison(slots)
    assert got["headline"] is None
    assert got["omitted"]["S3"] == ["slot B: OOMKilled not recorded"]
